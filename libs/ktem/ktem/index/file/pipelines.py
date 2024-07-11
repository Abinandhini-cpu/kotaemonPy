from __future__ import annotations

import logging
import shutil
import time
import warnings
from collections import defaultdict
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Generator, Optional, Sequence

import tiktoken
from ktem.db.models import engine
from ktem.embeddings.manager import embedding_models_manager
from ktem.llms.manager import llms
from llama_index.core.readers.base import BaseReader
from llama_index.core.readers.file.base import default_file_metadata_func
from llama_index.core.vector_stores import (
    FilterCondition,
    FilterOperator,
    MetadataFilter,
    MetadataFilters,
)
from llama_index.core.vector_stores.types import VectorStoreQueryMode
from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from theflow.settings import settings
from theflow.utils.modules import import_dotted_string

from kotaemon.base import BaseComponent, Document, Node, Param, RetrievedDocument
from kotaemon.embeddings import BaseEmbeddings
from kotaemon.indices import VectorIndexing, VectorRetrieval
from kotaemon.indices.ingests.files import KH_DEFAULT_FILE_EXTRACTORS
from kotaemon.indices.rankings import (
    BaseReranking,
    CohereReranking,
    LLMReranking,
    LLMTrulensScoring,
)
from kotaemon.indices.splitters import BaseSplitter, TokenSplitter

from .base import BaseFileIndexIndexing, BaseFileIndexRetriever

logger = logging.getLogger(__name__)


@lru_cache
def dev_settings():
    """Retrieve the developer settings from flowsettings.py"""
    file_extractors = {}

    if hasattr(settings, "FILE_INDEX_PIPELINE_FILE_EXTRACTORS"):
        file_extractors = {
            key: import_dotted_string(value, safe=False)()
            for key, value in settings.FILE_INDEX_PIPELINE_FILE_EXTRACTORS.items()
        }

    chunk_size = None
    if hasattr(settings, "FILE_INDEX_PIPELINE_SPLITTER_CHUNK_SIZE"):
        chunk_size = settings.FILE_INDEX_PIPELINE_SPLITTER_CHUNK_SIZE

    chunk_overlap = None
    if hasattr(settings, "FILE_INDEX_PIPELINE_SPLITTER_CHUNK_OVERLAP"):
        chunk_overlap = settings.FILE_INDEX_PIPELINE_SPLITTER_CHUNK_OVERLAP

    return file_extractors, chunk_size, chunk_overlap


_default_token_func = tiktoken.encoding_for_model("gpt-3.5-turbo").encode


class DocumentRetrievalPipeline(BaseFileIndexRetriever):
    """Retrieve relevant document

    Args:
        vector_retrieval: the retrieval pipeline that return the relevant documents
            given a text query
        reranker: the reranking pipeline that re-rank and filter the retrieved
            documents
        get_extra_table: if True, for each retrieved document, the pipeline will look
            for surrounding tables (e.g. within the page)
        top_k: number of documents to retrieve
        mmr: whether to use mmr to re-rank the documents
    """

    embedding: BaseEmbeddings
    rerankers: Sequence[BaseReranking] = [LLMReranking.withx()]
    get_extra_table: bool = False
    mmr: bool = False
    top_k: int = 5
    retrieval_mode: str = "hybrid"

    @Node.auto(depends_on=["embedding", "VS", "DS"])
    def vector_retrieval(self) -> VectorRetrieval:
        return VectorRetrieval(
            embedding=self.embedding,
            vector_store=self.VS,
            doc_store=self.DS,
            retrieval_mode=self.retrieval_mode,  # type: ignore
            rerankers=self.rerankers,
        )

    def run(
        self,
        text: str,
        doc_ids: Optional[list[str]] = None,
        *args,
        **kwargs,
    ) -> list[RetrievedDocument]:
        """Retrieve document excerpts similar to the text

        Args:
            text: the text to retrieve similar documents
            doc_ids: list of document ids to constraint the retrieval
        """
        print("searching in doc_ids", doc_ids)
        if not doc_ids:
            logger.info(f"Skip retrieval because of no selected files: {self}")
            return []

        retrieval_kwargs: dict = {}
        with Session(engine) as session:
            stmt = select(self.Index).where(
                self.Index.relation_type == "vector",
                self.Index.source_id.in_(doc_ids),
            )
            results = session.execute(stmt)
            vs_ids = [r[0].target_id for r in results.all()]

        # with Session(engine) as session:
        #     stmt = select(self.Source).where(
        #         self.Source.id.in_(doc_ids),
        #     )
        #     results = session.execute(stmt)
        #     file_names = [r[0].name for r in results.all()]

        # do first round top_k extension
        retrieval_kwargs["do_extend"] = True
        retrieval_kwargs["scope"] = vs_ids
        retrieval_kwargs["filters"] = MetadataFilters(
            filters=[
                MetadataFilter(
                    key="file_id",
                    value=doc_ids,
                    operator=FilterOperator.IN,
                )
            ],
            condition=FilterCondition.OR,
        )

        if self.mmr:
            # TODO: double check that llama-index MMR works correctly
            retrieval_kwargs["mode"] = VectorStoreQueryMode.MMR
            retrieval_kwargs["mmr_threshold"] = 0.5

        # rerank
        s_time = time.time()
        print(f"retrieval_kwargs: {retrieval_kwargs.keys()}")
        docs = self.vector_retrieval(text=text, top_k=self.top_k, **retrieval_kwargs)
        print("retrieval step took", time.time() - s_time)

        if not self.get_extra_table:
            return docs

        # retrieve extra nodes relate to table
        table_pages = defaultdict(list)
        retrieved_id = set([doc.doc_id for doc in docs])
        for doc in docs:
            if "page_label" not in doc.metadata:
                continue
            if "file_name" not in doc.metadata:
                warnings.warn(
                    "file_name not in metadata while page_label is in metadata: "
                    f"{doc.metadata}"
                )
            table_pages[doc.metadata["file_name"]].append(doc.metadata["page_label"])

        queries: list[dict] = [
            {"$and": [{"file_name": {"$eq": fn}}, {"page_label": {"$in": pls}}]}
            for fn, pls in table_pages.items()
        ]
        if queries:
            extra_docs = self.vector_retrieval(
                text="",
                top_k=50,
                where=queries[0] if len(queries) == 1 else {"$or": queries},
            )
            for doc in extra_docs:
                if doc.doc_id not in retrieved_id:
                    docs.append(doc)

        return docs

    @classmethod
    def get_user_settings(cls) -> dict:
        from ktem.llms.manager import llms

        try:
            reranking_llm = llms.get_default_name()
            reranking_llm_choices = list(llms.options().keys())
        except Exception as e:
            logger.error(e)
            reranking_llm = None
            reranking_llm_choices = []

        return {
            "reranking_llm": {
                "name": "LLM for reranking",
                "value": reranking_llm,
                "component": "dropdown",
                "choices": reranking_llm_choices,
                "special_type": "llm",
            },
            "num_retrieval": {
                "name": "Number of document chunks to retrieve",
                "value": 10,
                "component": "number",
            },
            "retrieval_mode": {
                "name": "Retrieval mode",
                "value": "hybrid",
                "choices": ["vector", "text", "hybrid"],
                "component": "dropdown",
            },
            "prioritize_table": {
                "name": "Prioritize table",
                "value": True,
                "choices": [True, False],
                "component": "checkbox",
            },
            "mmr": {
                "name": "Use MMR",
                "value": False,
                "choices": [True, False],
                "component": "checkbox",
            },
            "use_reranking": {
                "name": "Use reranking (Cohere)",
                "value": True,
                "choices": [True, False],
                "component": "checkbox",
            },
        }

    @classmethod
    def get_pipeline(cls, user_settings, index_settings, selected):
        """Get retriever objects associated with the index

        Args:
            settings: the settings of the app
            kwargs: other arguments
        """
        retriever = cls(
            get_extra_table=user_settings["prioritize_table"],
            top_k=user_settings["num_retrieval"],
            mmr=user_settings["mmr"],
            embedding=embedding_models_manager[
                index_settings.get(
                    "embedding", embedding_models_manager.get_default_name()
                )
            ],
            retrieval_mode=user_settings["retrieval_mode"],
            rerankers=[CohereReranking(), LLMTrulensScoring()],
        )
        if not user_settings["use_reranking"]:
            retriever.rerankers = [LLMTrulensScoring()]  # type: ignore

        for reranker in retriever.rerankers:
            if isinstance(reranker, LLMReranking):
                reranker.llm = llms.get(
                    user_settings["reranking_llm"], llms.get_default()
                )

        kwargs = {".doc_ids": selected}
        retriever.set_run(kwargs, temp=False)
        return retriever


class IndexPipeline(BaseComponent):
    """Index a single file"""

    loader: BaseReader
    splitter: BaseSplitter
    chunk_batch_size: int = 50

    Source = Param(help="The SQLAlchemy Source table")
    Index = Param(help="The SQLAlchemy Index table")
    VS = Param(help="The VectorStore")
    DS = Param(help="The DocStore")
    FSPath = Param(help="The file storage path")
    user_id = Param(help="The user id")
    private: bool = False
    embedding: BaseEmbeddings

    @Node.auto(depends_on=["Source", "Index", "embedding"])
    def vector_indexing(self) -> VectorIndexing:
        return VectorIndexing(
            vector_store=self.VS, doc_store=self.DS, embedding=self.embedding
        )

    def handle_docs(self, docs, file_id, file_name) -> Generator[Document, None, int]:
        chunks = []
        n_chunks = 0

        text_docs = []
        non_text_docs = []
        for doc in docs:
            # doc.metadata['file_id'] = file_id

            if doc.metadata.get("type", "text") == "text":
                text_docs.append(doc)
            else:
                non_text_docs.append(doc)

        for cidx, chunk in enumerate(self.splitter(text_docs)):
            chunks.append(chunk)
            if cidx % self.chunk_batch_size == 0:
                self.handle_chunks(chunks, file_id)
                n_chunks += len(chunks)
                chunks = []
                yield Document(
                    f" => [{file_name}] Processed {n_chunks} chunks", channel="debug"
                )

        chunks += non_text_docs
        if chunks:
            self.handle_chunks(chunks, file_id)
            n_chunks += len(chunks)
            yield Document(
                f" => [{file_name}] Processed {n_chunks} chunks", channel="debug"
            )

        return n_chunks

    def handle_chunks(self, chunks, file_id):
        """Run chunks"""
        # run embedding, add to both vector store and doc store
        self.vector_indexing(chunks)

        # record in the index
        with Session(engine) as session:
            nodes = []
            for chunk in chunks:
                nodes.append(
                    self.Index(
                        source_id=file_id,
                        target_id=chunk.doc_id,
                        relation_type="document",
                    )
                )
                nodes.append(
                    self.Index(
                        source_id=file_id,
                        target_id=chunk.doc_id,
                        relation_type="vector",
                    )
                )
            session.add_all(nodes)
            session.commit()

    def get_id_if_exists(self, file_path: Path) -> Optional[str]:
        """Check if the file is already indexed

        Args:
            file_path: the path to the file

        Returns:
            the file id if the file is indexed, otherwise None
        """
        if self.private:
            cond: tuple = (
                self.Source.name == file_path.name,
                self.Source.user == self.user_id,
            )
        else:
            cond = (self.Source.name == file_path.name,)

        with Session(engine) as session:
            stmt = select(self.Source).where(*cond)
            item = session.execute(stmt).first()
            if item:
                return item[0].id

        return None

    def store_file(self, file_path: Path) -> str:
        """Store file into the database and storage, return the file id

        Args:
            file_path: the path to the file

        Returns:
            the file id
        """
        with file_path.open("rb") as fi:
            file_hash = sha256(fi.read()).hexdigest()

        shutil.copy(file_path, self.FSPath / file_hash)
        source = self.Source(
            name=file_path.name,
            path=file_hash,
            size=file_path.stat().st_size,
            user=self.user_id,  # type: ignore
        )
        with Session(engine) as session:
            session.add(source)
            session.commit()
            file_id = source.id

        return file_id

    def finish(self, file_id: str, file_path: Path) -> str:
        """Finish the indexing"""
        with Session(engine) as session:
            stmt = select(self.Source).where(self.Source.id == file_id)
            result = session.execute(stmt).first()
            if not result:
                return file_id

            item = result[0]

            # populate the number of tokens
            doc_ids_stmt = select(self.Index.target_id).where(
                self.Index.source_id == file_id,
                self.Index.relation_type == "document",
            )
            doc_ids = [_[0] for _ in session.execute(doc_ids_stmt)]
            token_func = self.get_token_func()
            if doc_ids and token_func:
                docs = self.DS.get(doc_ids)
                item.note["tokens"] = sum([len(token_func(doc.text)) for doc in docs])

            # populate the note
            item.note["loader"] = self.get_from_path("loader").__class__.__name__

            session.add(item)
            session.commit()

        return file_id

    def get_token_func(self):
        """Get the token function for calculating the number of tokens"""
        return _default_token_func

    def delete_file(self, file_id: str):
        """Delete a file from the db, including its chunks in docstore and vectorstore

        Args:
            file_id: the file id
        """
        with Session(engine) as session:
            session.execute(delete(self.Source).where(self.Source.id == file_id))
            vs_ids, ds_ids = [], []
            index = session.execute(
                select(self.Index).where(self.Index.source_id == file_id)
            ).all()
            for each in index:
                if each[0].relation_type == "vector":
                    vs_ids.append(each[0].target_id)
                else:
                    ds_ids.append(each[0].target_id)
                session.delete(each[0])
            session.commit()

        if vs_ids:
            self.VS.delete(vs_ids)
        if ds_ids:
            self.DS.delete(ds_ids)

    def run(self, file_path: str | Path, reindex: bool, **kwargs) -> str:
        """Index the file and return the file id"""
        # check for duplication
        file_path = Path(file_path).resolve()
        file_id = self.get_id_if_exists(file_path)
        if file_id is not None:
            if not reindex:
                raise ValueError(
                    f"File {file_path.name} already indexed. Please rerun with "
                    "reindex=True to force reindexing."
                )
            else:
                # remove the existing records
                self.delete_file(file_id)
                file_id = self.store_file(file_path)
        else:
            # add record to db
            file_id = self.store_file(file_path)

        # extract the file
        extra_info = default_file_metadata_func(str(file_path))
        extra_info["file_id"] = file_id

        docs = self.loader.load_data(file_path, extra_info=extra_info)
        for _ in self.handle_docs(docs, file_id, file_path.name):
            continue
        self.finish(file_id, file_path)

        return file_id

    def stream(
        self, file_path: str | Path, reindex: bool, **kwargs
    ) -> Generator[Document, None, str]:
        # check for duplication
        file_path = Path(file_path).resolve()
        file_id = self.get_id_if_exists(file_path)
        if file_id is not None:
            if not reindex:
                raise ValueError(
                    f"File {file_path.name} already indexed. Please rerun with "
                    "reindex=True to force reindexing."
                )
            else:
                # remove the existing records
                yield Document(f" => Removing old {file_path.name}", channel="debug")
                self.delete_file(file_id)
                file_id = self.store_file(file_path)
        else:
            # add record to db
            file_id = self.store_file(file_path)

        # extract the file
        extra_info = default_file_metadata_func(str(file_path))
        extra_info["file_id"] = file_id

        yield Document(f" => Converting {file_path.name} to text", channel="debug")
        docs = self.loader.load_data(file_path, extra_info=extra_info)
        yield Document(f" => Converted {file_path.name} to text", channel="debug")
        yield from self.handle_docs(docs, file_id, file_path.name)

        self.finish(file_id, file_path)

        yield Document(f" => Finished indexing {file_path.name}", channel="debug")
        return file_id


class IndexDocumentPipeline(BaseFileIndexIndexing):
    """Index the file. Decide which pipeline based on the file type.

    This method is essentially a factory to decide which indexing pipeline to use.

    We can decide the pipeline programmatically, and/or automatically based on an LLM.
    If we based on the LLM, essentially we will log the LLM thought process in a file,
    and then during the indexing, we will read that file to decide which pipeline
    to use, and then log the operation in that file. Overtime, the LLM can learn to
    decide which pipeline should be used.
    """

    embedding: BaseEmbeddings

    @classmethod
    def get_pipeline(cls, user_settings, index_settings) -> BaseFileIndexIndexing:
        obj = cls(
            embedding=embedding_models_manager[
                index_settings.get(
                    "embedding", embedding_models_manager.get_default_name()
                )
            ]
        )
        return obj

    def route(self, file_path: Path) -> IndexPipeline:
        """Decide the pipeline based on the file type

        Can subclass this method for a more elaborate pipeline routing strategy.
        """
        readers, chunk_size, chunk_overlap = dev_settings()

        ext = file_path.suffix
        reader = readers.get(ext, KH_DEFAULT_FILE_EXTRACTORS.get(ext, None))
        if reader is None:
            raise NotImplementedError(
                f"No supported pipeline to index {file_path.name}. Please specify "
                "the suitable pipeline for this file type in the settings."
            )

        pipeline: IndexPipeline = IndexPipeline(
            loader=reader,
            splitter=TokenSplitter(
                chunk_size=chunk_size or 1024,
                chunk_overlap=chunk_overlap or 256,
                separator="\n\n",
                backup_separators=["\n", ".", "\u200B"],
            ),
            Source=self.Source,
            Index=self.Index,
            VS=self.VS,
            DS=self.DS,
            FSPath=self.FSPath,
            user_id=self.user_id,
            private=self.private,
            embedding=self.embedding,
        )

        return pipeline

    def run(
        self, file_paths: str | Path | list[str | Path], reindex: bool = False, **kwargs
    ) -> tuple[list[str | None], list[str | None]]:
        """Return a list of indexed file ids, and a list of errors"""
        if not isinstance(file_paths, list):
            file_paths = [file_paths]

        file_ids: list[str | None] = []
        errors: list[str | None] = []
        for file_path in file_paths:
            file_path = Path(file_path)

            try:
                pipeline = self.route(file_path)
                file_id = pipeline.run(file_path, reindex=reindex, **kwargs)
                file_ids.append(file_id)
                errors.append(None)
            except Exception as e:
                logger.error(e)
                file_ids.append(None)
                errors.append(str(e))

        return file_ids, errors

    def stream(
        self, file_paths: str | Path | list[str | Path], reindex: bool = False, **kwargs
    ) -> Generator[Document, None, tuple[list[str | None], list[str | None]]]:
        """Return a list of indexed file ids, and a list of errors"""
        if not isinstance(file_paths, list):
            file_paths = [file_paths]

        file_ids: list[str | None] = []
        errors: list[str | None] = []
        n_files = len(file_paths)
        for idx, file_path in enumerate(file_paths):
            file_path = Path(file_path)
            yield Document(
                content=f"Indexing [{idx+1}/{n_files}]: {file_path.name}",
                channel="debug",
            )

            try:
                pipeline = self.route(file_path)
                file_id = yield from pipeline.stream(
                    file_path, reindex=reindex, **kwargs
                )
                file_ids.append(file_id)
                errors.append(None)
                yield Document(
                    content={"file_path": file_path, "status": "success"},
                    channel="index",
                )
            except Exception as e:
                logger.exception(e)
                file_ids.append(None)
                errors.append(str(e))
                yield Document(
                    content={
                        "file_path": file_path,
                        "status": "failed",
                        "message": str(e),
                    },
                    channel="index",
                )

        return file_ids, errors
