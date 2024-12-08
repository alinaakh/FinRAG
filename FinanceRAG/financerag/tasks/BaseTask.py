import csv
import json
import logging
import os
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

import pytrec_eval
from tqdm import tqdm, trange
from financerag.common import Generator, HFDataLoader, Reranker, Retrieval
from financerag.tasks.TaskMetadata import TaskMetadata
import asyncio, nest_asyncio
import lancedb, openai
from lancedb.pydantic import LanceModel, Vector
from lancedb.embeddings import EmbeddingFunctionRegistry,get_registry
from lancedb.rerankers import ColbertReranker, CohereReranker, JinaReranker
import re, tiktoken, pandas, time
from tqdm.asyncio import tqdm as async_tqdm
from functools import lru_cache

logging.getLogger("openai").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.ERROR)
nest_asyncio.apply()

OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
logger = logging.getLogger(__name__)
if not logger.handlers:
    logger.setLevel(logging.INFO)
    console_handler = logging.StreamHandler()
    formatter = logging.Formatter('%(name)s - %(levelname)s - %(asctime)s - %(message)s')
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    logger.propagate = False

# Adapted from https://github.com/embeddings-benchmark/mteb/blob/main/mteb/abstasks/AbsTask.py
class BaseTask:
    """
    Base class for handling tasks related to document retrieval, reranking, and generation in the finance domain.
    The class loads data, handles retrieval and reranking operations, and can generate results using a language model.

    Attributes:
        metadata (`TaskMetadata`):
            Metadata containing task-specific information, such as dataset paths and subsets.
        queries (`Optional[Dict[str, str]]`, defaults to `None`):
            A dictionary mapping query IDs to query text.
        corpus (`Optional[Dict[str, Dict[str, str]]]`, defaults to `None`):
            A dictionary mapping document IDs to a dictionary containing the document title and text.
        retrieve_results (`Optional[Dict]`, defaults to `None`):
            The results of the retrieval process.
        rerank_results (`Optional[Dict]`, defaults to `None`):
            The results of the reranking process.
        generate_results (`Optional[Dict]`, defaults to `None`):
            The results generated by the model.

    Methods:
        load_data():
            Loads the dataset (queries and corpus) into memory from the provided metadata.
        retrieve(retriever: Retrieval, top_k: Optional[int] = 100, **kwargs):
            Performs document retrieval based on the given queries and corpus.
        rerank(reranker: Reranker, results: Optional[Dict] = None, top_k: Optional[int] = 100, batch_size: Optional[int] = None, **kwargs):
            Reranks the retrieved results using the given reranker model.
        generate(model: Generator, results: Optional[Dict] = None, prepare_messages: Optional[Callable] = None, **kwargs):
            Generates results based on the highest-scoring documents from the reranked or retrieved results.
        prepare_generation_inputs(results: Dict, prepare_messages: Callable) -> Dict[str, List[dict]]:
            Prepares the input format required for generating results by the model.
        save_results(top_k: int = 10, output_dir: Optional[str] = None) -> None:
            Saves the results (retrieval, reranking, and generated) to CSV and JSONL files.
    """

    def __init__(self, metadata: TaskMetadata):
        """
        Initializes the BaseTask class with metadata for loading and processing retrieval tasks.

        Args:
            metadata (`TaskMetadata`):
                Task-specific metadata that contains dataset information and configurations.
        """
        self.metadata: TaskMetadata = metadata
        self.queries: Optional[Dict[str, str]] = None
        self.corpus: Optional[Dict[str, Dict[Literal["title", "text"], str]]] = None
        self.retrieve_results: Optional[Dict] = None
        self.rerank_results: Optional[Dict] = None
        self.generate_results: Optional[Dict] = None
        self.openai_embedder = get_registry().get("openai").create(name="text-embedding-3-small") #get_registry().get("sentence-transformers").create(name="BAAI/bge-small-en-v1.5", device="cpu")
        self.reranker = ColbertReranker() #CohereReranker(api_key=os.getenv('CO_API_KEY')) #JinaReranker(api_key=JINA_API_KEY)#
        self.token_encoder = tiktoken.get_encoding("cl100k_base")
        self.hybrid_retriever = None
        self.table = None
        self.Schema = self._define_schema() # hybrid search table schema
        self.client = openai.OpenAI()#
        self.load_data()

    def _define_schema(self):
        class TextSchema(LanceModel):
            doc_id: str
            find_id: str
            title: str
            text: str = self.openai_embedder.SourceField()
            vector: Vector(self.openai_embedder.ndims()) = self.openai_embedder.VectorField()
        return TextSchema

    @property
    def metadata_dict(self) -> Dict[str, Any]:
        """
        Converts the task metadata into a dictionary format.

        Returns:
            `Dict[str, Any]`:
                A dictionary representation of the task metadata.
        """
        return dict(self.metadata)

    def load_data(self):
        """
        Loads the corpus and queries from the specified dataset path and subset in the metadata.

        Raises:
            `ValueError`:
                If the dataset cannot be loaded from the specified path and subset.
        """
        if (self.corpus is None) or (self.queries is None):
            dataset_path = self.metadata_dict["dataset"]["path"]
            subset = self.metadata_dict["dataset"]["subset"]

            corpus, queries = HFDataLoader(
                hf_repo=dataset_path,
                subset=subset,
                keep_in_memory=False,
            ).load()

            self.queries = {query["id"]: query["text"] for query in queries}
            self.corpus = {
                doc["id"]: {"title": doc["title"], "text": doc["text"]}
                for doc in corpus
            }

    def create_hybrid_retriever(self, mode: str = "overwrite", batch_size: int = 64):
        if (self.hybrid_retriever is None):
            logger.info("Creating hybrid search table")
            db = lancedb.connect("/tmp/.lancedb")
            self.hybrid_retriever = db.create_table("hybrid_search_table", schema=self.Schema, on_bad_vectors="drop", mode=mode)
            # Add the corpus to the table
            def get_find_id(doc_id):
                # Extract sequences of uppercase letters
                uppercase_seq = ''.join([char for char in doc_id if char.isupper()])
                return uppercase_seq if uppercase_seq else "random"
            corpus_list = list(self.corpus.items())
            for i in trange(0, len(corpus_list), batch_size, desc="Adding corpus to hybrid search table"):
                if i + batch_size <= len(corpus_list):
                    batch = corpus_list[i:i+batch_size]
                else:
                    batch = corpus_list[i:]
                
                self.hybrid_retriever.add(data=[{
                                "doc_id": doc_id,
                                "find_id": get_find_id(doc_id),
                                "title": doc_data["title"],
                                "text": self._clean_text("; ".join(doc_data["title"].split('_')) \
                                                         + "\n" + doc_data["text"]),
                    } for doc_id, doc_data in batch],
                    on_bad_vectors="drop")
            try:
                self.hybrid_retriever.create_fts_index(['title', 'text'], replace=True)
            except Exception as e:
                logger.warning(f"Failed to create FTS index: {e}")

    def _clean_text(self, string: str, max_tokens: int = 8192) -> str:
    
        # Decode using UTF-8, replacing invalid bytes
        clean_string = string.encode('utf-8', 'replace').decode('utf-8', 'replace')
        # Remove substrings enclosed in <| and |> - AI special tokens
        #clean_string = re.sub(r'<\|.*?\|>', '', clean_string)
        # Optionally, take out ALL the extra space runs, like code indenting
        clean_string = ' '.join(clean_string.split())
        # truncate to max_tokens 
        toks = self.token_encoder.encode(clean_string)
        if len(toks) > max_tokens:
            clean_string = self.token_encoder.decode(toks[:max_tokens])
        return clean_string
    
    def _remove_punctuation(self, string: str) -> str:
        pattern = "[!\"#&*+,/:;<=>?@[\]^_`{|}~]" # some punctuation characters are not removed
        return re.sub(pattern, '', string)
    
    @lru_cache(maxsize=1000)
    def keyword_extraction_expansion(self, query: str, top_k: int = 100) -> List[str]:
        # use openai to extract keywords from the query
        response = self.client.chat.completions.create(
            model="gpt-4o-mini",
            seed=42,
            messages=[{"role": "system", "content": 
                       """Extract search keywords from input query, focus on financial terms, companies, person names, and dates. 
                       Expand the query by adding more keywords that are semantically related to the query, consider synonyms, related terms, and variations to broaden the scope.
                       Ensure the extracted and expanded search keywords are relevant to a finance domain. Avoid generic terms like "financial statement".
                       Output keywords in a comma-separated list, nothing else."""
                       },
                    {"role": "user", "content": f"Input query: {query}"}],
            max_tokens=256,
        ).choices[0].message.content
        return response.split(', ')
    
    def _rename_score_column(self, df: pandas.DataFrame, new_name: str) -> bool:
        if  (new_name in df.columns) or (len(df) == 0): # column already exists
            return False
        
        if "_score" in df.columns:
            df.rename(columns={"_score": new_name}, inplace=True)
        elif "_relevance_score" in df.columns:
            df.rename(columns={"_relevance_score": new_name}, inplace=True)
        else:
            raise ValueError(f"No score column found in DataFrame columns: {df.columns}")
        return True

    def hybrid_retrieve_rerank(self, top_k=100, query_ids=None, alpha=0.3, **kwargs) -> Dict[str, Dict[str, float]]:
        logger.info("Starting hybrid search with semantic and full-text similarity.")
        if query_ids:
            logger.info(f"Processing {len(query_ids)} specified queries.")
        else:
            logger.info("Processing all queries.")
    
        results = {}
    
        for q_id, query in tqdm(self.queries.items(), desc="Hybrid search", total=len(self.queries)):
            if query_ids and q_id not in query_ids:
                continue
    
            retrieved_docs_1 = pandas.DataFrame()  # Default empty DataFrame for FTS results
            
            # Step 1: Full-text search
            try:
                keywords = self.keyword_extraction_expansion(query)
                query_kw = "; ".join(keywords)
                retrieved_docs_1 = (
                    self.hybrid_retriever
                    .search(query=query_kw, query_type='fts')
                    .rerank(reranker=self.reranker)
                    .limit(top_k)
                    .to_pandas()
                )
                _ = self._rename_score_column(retrieved_docs_1, "score")
                logger.info(f"FTS successful for query ID {q_id}. Retrieved {len(retrieved_docs_1)} documents.")
            except Exception as e:
                logger.warning(f"FTS failed for query ID {q_id}: {e}")
            
            # Step 2: Hybrid or fallback vector search
            try:
                retrieved_docs = (
                    self.hybrid_retriever
                    .search(query=query, query_type='hybrid')
                    .rerank(reranker=self.reranker)
                    .limit(top_k)
                    .to_pandas()
                )
            except Exception as e:
                logger.warning(f"Hybrid search failed for query ID {q_id}: {e}. Retrying with vector search.")
                try:
                    retrieved_docs = (
                        self.hybrid_retriever
                        .search(query=query, query_type='vector')
                        .rerank(reranker=self.reranker)
                        .limit(top_k)
                        .to_pandas()
                    )
                except Exception as vector_error:
                    logger.error(f"Vector search also failed for query ID {q_id}: {vector_error}")
                    results[q_id] = {}  # No documents retrieved
                    continue
            
            # Combine results if FTS was successful
            if not retrieved_docs_1.empty:
                retrieved_docs = pandas.concat([retrieved_docs_1, retrieved_docs], ignore_index=True)
                retrieved_docs.drop_duplicates(subset=['doc_id'], keep='first', inplace=True)
    
            # Clean up and finalize results
            retrieved_docs.dropna(inplace=True)
            _ = self._rename_score_column(retrieved_docs, "score")
            retrieved_docs = retrieved_docs.sort_values(by='score', ascending=False).reset_index(drop=True)
    
            results[q_id] = {doc_id: score for doc_id, score in zip(retrieved_docs['doc_id'], retrieved_docs['score'])}
            logger.info(f"Query ID {q_id} retrieved {len(results[q_id])} documents.")
    
        return results




    '''
    
    async def async_process_query(self, q_id: str, query: str, top_k: int) -> Tuple[str, Dict[str, float]]:
        
        try:
            retrieved_docs = self.hybrid_retriever.search(query=query, query_type='hybrid').rerank(reranker=self.reranker).limit(top_k).to_pandas()
        except Exception as e:
            retrieved_docs = self.hybrid_retriever.search(query=query, query_type='vector').rerank(reranker=self.reranker).limit(top_k).to_pandas()
        
        retrieved_docs = retrieved_docs.sort_values(by='_relevance_score', ascending=False).drop_duplicates(subset=['doc_id'], keep='first').reset_index(drop=True)
        
        return q_id, {doc_id: score for doc_id, score in zip(retrieved_docs['doc_id'], retrieved_docs['_relevance_score'])}
    
    async def async_hybrid_retrieve_rerank(self, top_k: int = 100, batch_size: int = 10, **kwargs) -> Dict[str, Dict[str, float]]:
        logger.info("Hybrid search with both semantic and full text similarity (async batch processing).")

        async def process_batch(batch):
            tasks = [self.async_process_query(q_id, query, top_k) for q_id, query in batch]
            return await asyncio.gather(*tasks)
            
            return results
        
        # Run the async queries
        all_results = {}
        query_items = list(self.queries.items())
        
        for i in trange(0, len(query_items), batch_size, desc="Processing queries in batches"):
            batch = query_items[i:i+batch_size]
            
            batch_results = await process_batch(batch)
            
            for q_id, result in batch_results:
                all_results[q_id] = result
      
        return all_results
    '''


    def retrieve(
            self, retriever: Retrieval, top_k: Optional[int] = 100, **kwargs
    ) -> Dict[str, Dict[str, float]]:
        """
        Performs document retrieval using the provided retriever model.

        Args:
            retriever (`Retrieval`):
                The retrieval model to use for retrieving documents.
            top_k (`Optional[int]`, defaults to `100`):
                The number of top results to return for each query.
            **kwargs:
                Additional keyword arguments for the retriever.

        Returns:
            `Dict[str, Dict[str, float]]`:
                A dictionary where the key is the query ID and the value is another dictionary
                mapping document IDs to their retrieval scores.

        Raises:
            `TypeError`:
                If the `retriever` is not a subclass of `Retrieval`.
            `ValueError`:
                If the data (corpus or queries) is not loaded before retrieval.
        """
        if not issubclass(type(retriever), Retrieval):
            raise TypeError(f"{type(retriever)} must be a subclass of the `Retrieval` class")

        if (self.corpus is None) or (self.queries is None):
            raise ValueError("Data has not been loaded.")

        self.retrieve_results = retriever.retrieve(
            queries=self.queries, corpus=self.corpus, top_k=top_k, **kwargs
        )

        return self.retrieve_results

    def rerank(
            self,
            reranker: Reranker,
            results: Optional[Dict[str, Dict[str, float]]] = None,
            top_k: int = 100,
            batch_size: Optional[int] = None,
            **kwargs,
    ) -> Dict[str, Dict[str, float]]:
        """
        Reranks the retrieved results using the provided reranker model.

        Args:
            reranker (`Reranker`):
                The reranker model to use for reranking the retrieved results.
            results (`Optional[Dict]`, defaults to `None`):
                The initial results to rerank. If not provided, the method uses the retrieval results.
            top_k (`Optional[int]`, defaults to `100`):
                The number of top results to return after reranking.
            batch_size (`Optional[int]`, defaults to `None`):
                The batch size to use for reranking.
            **kwargs:
                Additional keyword arguments for the reranker.

        Returns:
            `Dict[str, Dict[str, float]]`:
                A dictionary where the key is the query ID and the value is another dictionary
                mapping document IDs to reranked scores.

        Raises:
            `TypeError`:
                If the `reranker` is not a subclass of `Reranker`.
            `ValueError`:
                If the data (corpus or queries) is not loaded before reranking or both `results` and `retrieve_results` are None.
        """
        if not issubclass(type(reranker), Reranker):
            raise TypeError(f"{type(reranker)} must be a subclass of the `Reranker` class")

        if (self.corpus is None) or (self.queries is None):
            raise ValueError("Data has not been loaded.")

        if results is None:
            if self.retrieve_results is not None:
                results = self.retrieve_results
            else:
                raise ValueError("Neither retrieve_results nor results can be None simultaneously.")

        self.rerank_results = reranker.rerank(
            queries=self.queries,
            corpus=self.corpus,
            results=results,
            top_k=top_k,
            batch_size=batch_size,
            **kwargs,
        )

        return self.rerank_results

    def generate(
            self,
            model: Generator,
            results: Optional[Dict] = None,
            prepare_messages: Optional[Callable] = None,
            **kwargs,
    ) -> Dict[str, str]:
        """
        Generates responses based on the highest-scoring documents from either the reranked or retrieved results.

        Args:
            model (`Generator`):
                The model used to generate responses.
            results (`Optional[Dict]`, defaults to `None`):
                The results to generate responses from. If not provided, uses reranked or retrieved results.
            prepare_messages (`Optional[Callable]`, defaults to `None`):
                A function to prepare messages for the generation model. If not provided, a default message
                preparation function is used.
            **kwargs:
                Additional keyword arguments for the generation process.

        Returns:
            `Dict[str, str]`:
                A dictionary where the key is the query ID and the value is the generated response.

        Raises:
            `TypeError`:
                If the `model` is not a subclass of `Generator`.
            `AssertionError`:
                If neither rerank_results nor retrieve_results are available for generating responses.
        """
        if not issubclass(type(model), Generator):
            raise TypeError(f"{type(model)} must be a subclass of the `Generator` class")

        if prepare_messages is None:
            logger.info(
                "No prepare_messages function provided. "
                "Using default message preparation function, which selects the highest scored document for each query."
            )

            def default_messages(
                    query: str, documents: List[Tuple[str, float]]
            ) -> List[Dict]:
                first_document = max(documents, key=lambda x: x[1])[0]
                messages = [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {
                        "role": "user",
                        "content": f"Document: {first_document}"
                                   f"\nGenerate an answer to the question from the document."
                                   f"\nQuestion: {query}",
                    },
                ]
                return messages

            prepare_messages = default_messages

        if results is None:
            results = (
                self.rerank_results
                if self.rerank_results is None
                else self.retrieve_results
            )
            assert results is not None, (
                "Neither rerank_results nor retrieve_results are available. "
                "One of them must be provided."
            )

        messages_dict = self.prepare_generation_inputs(results, prepare_messages)
        self.generate_results = model.generation(messages_dict, **kwargs)

        return self.generate_results

    def prepare_generation_inputs(
            self, results, prepare_messages
    ) -> Dict[str, List[dict]]:
        """
        Prepares the input messages required for the generation model.

        Args:
            results (`Dict`):
                The results from retrieval or reranking, which are used to generate responses.
            prepare_messages (`Callable`):
                A function that prepares the messages required for the generation model.

        Returns:
            `Dict[str, List[dict]]`:
                A dictionary where the key is the query ID and the value is a list of messages (dictionaries)
                that will be passed to the generation model.

        Raises:
            `ValueError`:
                If the data (corpus or queries) is not loaded.
        """
        if (self.corpus is None) or (self.queries is None):
            raise ValueError("Data has not been loaded.")

        messages_dict: Dict[str, List[Dict[str, str]]] = {}
        logger.info("Preparing generation inputs for %d queries.", len(results))
        for query_id, result in results.items():
            query = self.queries[query_id]
            documents = [
                (self.corpus[doc_id], score) for doc_id, score in result.items()
            ]
            messages = prepare_messages(query, documents)
            messages_dict[query_id] = messages

        logger.info("Successfully prepared generation inputs for all queries.")
        return messages_dict

    def save_results(self, top_k: int = 10, output_dir: Optional[str] = None) -> None:
        """
        Saves the top retrieval or reranking, and generated results to CSV and JSONL files.

        Args:
            top_k (`int`, defaults to `10`):
                The number of top results to save for each query.
            output_dir (`Optional[str]`, defaults to `None`):
                The directory where the results should be saved. If not provided, results are not saved.

        Saves:
            - Top `top_k` retrieval or reranked results in CSV format.
            - Generated responses in JSONL format.
        """
        # If no output directory is provided, stop saving.
        if output_dir is None:
            return
        # Create the output directory if it does not exist
        output_dir = os.path.join(output_dir, self.metadata.name)
        os.makedirs(output_dir, exist_ok=True)

        logger.info(f"Output directory set to: {output_dir}")

        # Path to save the CSV file
        csv_file_path = os.path.join(output_dir, "results.csv")
        jsonl_file_path = os.path.join(output_dir, "results_output.jsonl")
        logger.info(f"Saving top {top_k} results to CSV file: {csv_file_path}")

        # Determine whether to use rerank results or retrieve results
        final_result = (
            self.rerank_results
            if self.rerank_results is not None
            else self.retrieve_results
        )

        # Process the final result if it's not None
        if final_result is not None:
            with open(jsonl_file_path, "w") as f:
                for q_id, doc_scores in final_result.items():
                    f.writelines(json.dumps({"query_id": q_id, "corpus_id": doc_id, "score": score}) + "\n"
                                 for doc_id, score in doc_scores.items())
            with open(csv_file_path, mode="w", newline="") as csv_file:
                writer = csv.writer(csv_file)
                # Write the header to the CSV file
                writer.writerow(["query_id", "corpus_id"])
                logger.info("Writing header ['query_id', 'corpus_id'] to CSV.")

                # For each query_id, save the top_k corpus_ids sorted by score
                for q_id, doc_scores in final_result.items():
                    # Sort doc_scores by score and select top_k documents
                    sorted_docs = sorted(
                        doc_scores.items(), key=lambda item: item[1], reverse=True
                    )[:top_k]

                    # Write the query_id and corpus_id to the CSV
                    for doc_id, _ in sorted_docs:
                        writer.writerow([q_id, doc_id])

            logger.info(f"Top {top_k} results saved successfully to {csv_file_path}")

        # Save generate_results to JSON Lines format
        if self.generate_results is not None:
            jsonl_file_path = os.path.join(output_dir, "output.jsonl")
            logger.info(f"Saving generate_results to JSONL file: {jsonl_file_path}")

            with open(jsonl_file_path, "w") as f:
                f.writelines(
                    json.dumps({"query_id": q_id, "answer": answer}) + "\n"
                    for q_id, answer in self.generate_results.items()
                )

            logger.info(f"generate_results saved successfully to {jsonl_file_path}")

    # adapted from https://github.com/beir-cellar/beir/blob/main/beir/retrieval/evaluation.py
    @staticmethod
    def evaluate(
            qrels: Dict[str, Dict[str, int]],
            results: Dict[str, Dict[str, float]],
            k_values: List[int],
            ignore_identical_ids: bool = True
    ) -> Tuple[Dict[str, float], Dict[str, float], Dict[str, float], Dict[str, float]]:

        if ignore_identical_ids:
            logger.info(
                'For evaluation, we ignore identical query and document ids (default), '
                'please explicitly set ``ignore_identical_ids=False`` to ignore this.')
            popped = []
            for qid, rels in results.items():
                for pid in list(rels):
                    if qid == pid:
                        results[qid].pop(pid)  # remove identical query-document pairs
                        popped.append(pid)

        # Filter results to only keep queries that are present in qrels
        filtered_results = {qid: rels for qid, rels in results.items() if qid in qrels}

        # Initialize dictionaries for evaluation metrics
        ndcg = {}
        _map = {}
        recall = {}
        precision = {}

        # Initialize metric values for each k in k_values
        for k in k_values:
            ndcg[f"NDCG@{k}"] = 0.0
            _map[f"MAP@{k}"] = 0.0
            recall[f"Recall@{k}"] = 0.0
            precision[f"P@{k}"] = 0.0

        # Define strings for pytrec_eval evaluation
        map_string = "map_cut." + ",".join([str(k) for k in k_values])
        ndcg_string = "ndcg_cut." + ",".join([str(k) for k in k_values])
        recall_string = "recall." + ",".join([str(k) for k in k_values])
        precision_string = "P." + ",".join([str(k) for k in k_values])

        # Perform evaluation using pytrec_eval with filtered results
        evaluator = pytrec_eval.RelevanceEvaluator(qrels,
                                                   {map_string, ndcg_string, recall_string, precision_string})
        scores = evaluator.evaluate(filtered_results)

        # Aggregate the scores for each query and each k
        for query_id in scores.keys():
            for k in k_values:
                ndcg[f"NDCG@{k}"] += scores[query_id]["ndcg_cut_" + str(k)]
                _map[f"MAP@{k}"] += scores[query_id]["map_cut_" + str(k)]
                recall[f"Recall@{k}"] += scores[query_id]["recall_" + str(k)]
                precision[f"P@{k}"] += scores[query_id]["P_" + str(k)]

        # Compute the average scores for each k
        for k in k_values:
            ndcg[f"NDCG@{k}"] = round(ndcg[f"NDCG@{k}"] / len(scores), 5)
            _map[f"MAP@{k}"] = round(_map[f"MAP@{k}"] / len(scores), 5)
            recall[f"Recall@{k}"] = round(recall[f"Recall@{k}"] / len(scores), 5)
            precision[f"P@{k}"] = round(precision[f"P@{k}"] / len(scores), 5)

        # Log the results for each metric
        for _eval in [ndcg, _map, recall, precision]:
            logger.info("\n")
            for k in _eval.keys():
                logger.info("{}: {:.4f}".format(k, _eval[k]))

        return ndcg, _map, recall, precision
