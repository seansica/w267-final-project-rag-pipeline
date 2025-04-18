#!/usr/bin/env python
"""
Experimental suite for RAG system evaluation with support for Ragas metrics.
This script runs a series of experiments with different configurations to find optimal settings.

How to Use the Script

Phase 1 (Embedding Model Selection):
python run_experiment_suite.py --phase 1 --max_parallel 2

Phase 2 (Chunking Strategy Selection):
# After analyzing Phase 1 results
python run_experiment_suite.py --phase 2 --embedding_model "multi-qa-mpnet-base-dot-v1" --max_parallel 2

Phase 3 (Retriever Method Selection):
# After analyzing Phase 2 results
python run_experiment_suite.py --phase 3 --embedding_model "multi-qa-mpnet-base-dot-v1" --chunk_size 256 --chunk_overlap 50 --max_parallel 2

To use Ragas metrics instead of custom evaluators, add the --ragas flag:
python run_experiment_suite.py --phase 1 --max_parallel 2 --ragas

To limit the number of questions used in evaluation (for faster testing):
python run_experiment_suite.py --phase 1 --max_parallel 2 --limit 10
"""
import sys
import os
import re
import time
import json
import argparse
import logging
import itertools
import numpy as np
from tqdm import tqdm
from datetime import datetime
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Any, Optional, Tuple, Union

# Load environment variables from .env file
load_dotenv()

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Import custom modules
from rag267.rag import RAGSystem
from rag267.vectordb.utils import Team, SupportedGeneratorModels, SupportedEmbeddingModels
from rag267.vectordb.manager import VectorDatabaseManager
from rag267.data_sources import data_sources

# Standard evaluators
from rag267.evals.correctness import correctness
from rag267.evals.relevance import relevance
from rag267.evals.retrieval_relevance import retrieval_relevance
from rag267.evals.groundedness import groundedness

# Ragas evaluators - Import the fixed versions
from rag267.evals.ragas.faithfulness import ragas_faithfulness
from rag267.evals.ragas.response_relevancy import ragas_response_relevancy
from rag267.evals.ragas.answer_accuracy import ragas_answer_accuracy
from rag267.evals.ragas.context_relevance import ragas_context_relevance

# Try to import DeepEval evaluators
try:
    from rag267.evals.deepeval.faithfulness_eval import deepeval_faithfulness
    from rag267.evals.deepeval.geval import deepeval_geval
    DEEPEVAL_AVAILABLE = True
except ImportError:
    logger.warning("DeepEval evaluators not available. Skip importing.")
    DEEPEVAL_AVAILABLE = False

# Try to import BERTScore evaluator
try:
    from rag267.evals.bertscore import bertscore_evaluator
    BERTSCORE_AVAILABLE = True
except ImportError:
    logger.warning("BERTScore evaluator not available. Skip importing.")
    BERTSCORE_AVAILABLE = False

from langsmith import Client


class ExperimentConfig:
    """Configuration for a single experiment"""
    def __init__(
        self,
        rag_type: str,
        team_type: str,
        embedding_model: str,
        chunk_size: int,
        chunk_overlap: int,
        retriever_type: str,
        top_k: Optional[int] = None,
        retriever_kwargs: Optional[Dict[str, Any]] = None,
        templates: Optional[Dict[str, str]] = None,
    ):
        self.rag_type = rag_type  # "cohere" or "mistral"
        self.team_type = team_type  # "engineering" or "marketing"
        self.embedding_model = embedding_model
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.retriever_type = retriever_type
        
        # Initialize retriever_kwargs
        self.retriever_kwargs = retriever_kwargs or {}
        
        # Handle top_k based on retriever type
        self.top_k = top_k
        # For similarity and mmr retriever types, ensure top_k is set
        if (retriever_type in ["similarity", "mmr", "multi_query"]) and top_k is not None:
            # Store top_k in retriever_kwargs if not already there
            if "k" not in self.retriever_kwargs:
                self.retriever_kwargs["k"] = top_k
        
        # Default templates
        self.templates = {
            "engineering": "templates/engineering_template_3.txt",
            "marketing": "templates/marketing_template_2.txt"
        }
        
        # Override with provided templates if any
        if templates:
            self.templates.update(templates)
            
    def get_experiment_id(self) -> str:
        """Generate a unique experiment ID from the configuration"""
        # Start with base experiment ID
        experiment_id = (f"v1-{self.rag_type}-{self.team_type}"
                        f"-emb-{self.embedding_model.split('/')[-1]}"
                        f"-cs{self.chunk_size}-co{self.chunk_overlap}")
        
        # Add retriever-specific information
        if self.retriever_type == "similarity":
            # For similarity retriever, include top_k if available
            if self.top_k is not None or "k" in self.retriever_kwargs:
                k_value = self.top_k if self.top_k is not None else self.retriever_kwargs.get("k")
                experiment_id += f"-k{k_value}"
        else:
            # For other retriever types, add type and any relevant parameters
            experiment_id += f"-{self.retriever_type}"
            
            # Add specific parameters for different retriever types
            if self.retriever_type == "similarity_score_threshold" and "score_threshold" in self.retriever_kwargs:
                experiment_id += f"-{self.retriever_kwargs['score_threshold']}"
            elif self.retriever_type in ["mmr", "multi_query"] and "k" in self.retriever_kwargs:
                experiment_id += f"-k{self.retriever_kwargs['k']}"
                
        return experiment_id
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary for serialization"""
        return {
            "rag_type": self.rag_type,
            "team_type": self.team_type,
            "embedding_model": self.embedding_model,
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "top_k": self.top_k,
            "retriever_type": self.retriever_type,
            "retriever_kwargs": self.retriever_kwargs,
            "templates": self.templates
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ExperimentConfig':
        """Create config from dictionary"""
        return cls(
            rag_type=data["rag_type"],
            team_type=data["team_type"],
            embedding_model=data["embedding_model"],
            chunk_size=data["chunk_size"],
            chunk_overlap=data["chunk_overlap"],
            retriever_type=data["retriever_type"],
            top_k=data.get("top_k", None),
            retriever_kwargs=data.get("retriever_kwargs", {}),
            templates=data.get("templates", None)
        )


def create_test_experiments() -> List[ExperimentConfig]:
    """Create Phase 0 (test) experiments: Embedding Model Comparison"""
    
    # Define the core parameters to test
    rag_models = ["cohere"]
    team_types = ["engineering"]
    
    # Embedding models to test
    embedding_models = [
        SupportedEmbeddingModels.MiniLmL6V2.value,
        SupportedEmbeddingModels.DistilRobertaV1.value
    ]
    
    # Define a focused matrix of experiments
    experiments = []
    
    logger.info("Creating Phase 0 (test) experiments - embedding model comparison")
    for rag_type, team_type, emb_model in itertools.product(rag_models, team_types, embedding_models):
        config = ExperimentConfig(
            rag_type=rag_type,
            team_type=team_type,
            embedding_model=emb_model,
            chunk_size=128,  # Baseline 
            chunk_overlap=0,  # Baseline
            retriever_type="similarity",  # Baseline
            top_k=4          # Baseline for similarity retriever
        )
        experiments.append(config)
    
    logger.info(f"Created {len(experiments)} experiments for Phase 0 (test)")
    return experiments

def create_phase1_experiments() -> List[ExperimentConfig]:
    """Create Phase 1 experiments: Embedding Model Comparison"""
    
    # Define the core parameters to test
    rag_models = ["cohere"] # optionally add mistral: ["cohere", "mistral"]
    team_types = ["engineering", "marketing"]
    
    # Embedding models to test
    embedding_models = [
        SupportedEmbeddingModels.MultiQaMpNetBasedDotV1.value,  # Baseline
        SupportedEmbeddingModels.MpNetBaseV2.value,
        SupportedEmbeddingModels.MiniLmL6V2.value,
        SupportedEmbeddingModels.DistilRobertaV1.value,
        SupportedEmbeddingModels.MultiQaMpNetBasedCosV1.value
    ]
    
    # Define a focused matrix of experiments
    experiments = []
    
    logger.info("Creating Phase 1 experiments - embedding model comparison")
    for rag_type, team_type, emb_model in itertools.product(rag_models, team_types, embedding_models):
        config = ExperimentConfig(
            rag_type=rag_type,
            team_type=team_type,
            embedding_model=emb_model,
            chunk_size=128,  # Baseline 
            chunk_overlap=0,  # Baseline
            retriever_type="similarity",  # Baseline
            top_k=4          # Baseline for similarity retriever
        )
        experiments.append(config)
    
    logger.info(f"Created {len(experiments)} experiments for Phase 1")
    return experiments


def create_phase2_experiments(best_embedding_model: str) -> List[ExperimentConfig]:
    """Create Phase 2 experiments: Chunk Size and Overlap Optimization"""
    
    # Define the core parameters to test
    rag_models = ["cohere"] # optionally add mistral: ["cohere", "mistral"]
    team_types = ["engineering", "marketing"]
    
    # Chunk sizes and overlaps to test
    chunk_sizes = [256, 512, 1024, 2048]
    chunk_overlaps = [0, 50, 100]
    
    # Define a focused matrix of experiments
    experiments = []
    
    logger.info("Creating Phase 2 experiments - chunk size and overlap test")
    
    for rag_type, team_type, size, overlap in itertools.product(
            rag_models, team_types, chunk_sizes, chunk_overlaps):
        # Skip redundant experiments (baseline already covered in Phase 1)
        if size == 128 and overlap == 0:
            continue
            
        config = ExperimentConfig(
            rag_type=rag_type,
            team_type=team_type,
            embedding_model=best_embedding_model,
            chunk_size=size,
            chunk_overlap=overlap,
            retriever_type="similarity",  # Baseline
            top_k=4  # Baseline for similarity retriever
        )
        experiments.append(config)
    
    logger.info(f"Created {len(experiments)} experiments for Phase 2")
    return experiments


def create_phase3_experiments(best_embedding_model: str, best_chunk_size: int, 
                             best_chunk_overlap: int) -> List[ExperimentConfig]:
    """Create Phase 3 experiments: Retriever Method Optimization"""
    
    # Define the core parameters to test
    rag_models = ["cohere"] # optionally add mistral: ["cohere", "mistral"]
    team_types = ["engineering", "marketing"]
    
    # Define a focused matrix of experiments
    experiments = []
    
    logger.info("Creating Phase 3 experiments - retriever type comparison")
    
    # Test different top_k values with similarity search
    for rag_type, team_type, k in itertools.product(rag_models, team_types, [8, 12]):
        config = ExperimentConfig(
            rag_type=rag_type,
            team_type=team_type, 
            embedding_model=best_embedding_model,
            chunk_size=best_chunk_size,
            chunk_overlap=best_chunk_overlap,
            retriever_type="similarity",
            top_k=k  # Testing different k values
        )
        experiments.append(config)
    
    # Test similarity search with score thresholds
    for rag_type, team_type, threshold in itertools.product(rag_models, team_types, [0.5, 0.8]):
        config = ExperimentConfig(
            rag_type=rag_type,
            team_type=team_type,
            embedding_model=best_embedding_model,
            chunk_size=best_chunk_size,
            chunk_overlap=best_chunk_overlap,
            retriever_type="similarity_score_threshold",
            top_k=4,  # Baseline for similarity threshold
            retriever_kwargs={"score_threshold": threshold}
        )
        experiments.append(config)
    
    # Test MMR retriever
    for rag_type, team_type in itertools.product(rag_models, team_types):
        config = ExperimentConfig(
            rag_type=rag_type,
            team_type=team_type,
            embedding_model=best_embedding_model,
            chunk_size=best_chunk_size,
            chunk_overlap=best_chunk_overlap,
            retriever_type="mmr",
            # For mmr, we include the k parameter in retriever_kwargs instead of top_k
            retriever_kwargs={"k": 4, "fetch_k": 8}  # Fetch more but return top 4
        )
        experiments.append(config)
    
    # Test multi-query retriever (one per LLM type)
    for rag_type, team_type in itertools.product(rag_models, team_types):
        # Use the same LLM for query generation as for the main RAG
        config = ExperimentConfig(
            rag_type=rag_type,
            team_type=team_type,
            embedding_model=best_embedding_model,
            chunk_size=best_chunk_size,
            chunk_overlap=best_chunk_overlap,
            retriever_type="multi_query",
            # For multi_query, we include the k parameter in retriever_kwargs
            retriever_kwargs={"llm_for_queries": rag_type, "k": 4}
        )
        experiments.append(config)
    
    logger.info(f"Created {len(experiments)} experiments for Phase 3")
    return experiments


def load_validation_data(path: str = None, limit: int = 78) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Load validation data and prepare examples for both teams."""
    logger.info("Loading validation data")
    validation_file = os.path.relpath(path) if path is not None else os.path.join("data", "validation_question_answers.json")
    with open(validation_file, "r") as f:
        validation_question_answers = json.load(f)

    # Transform the data into LangSmith compatible examples
    examples_engineering = []
    examples_marketing = []

    counter = 0
    for sample in validation_question_answers.values():
        
        if counter == limit:
            return examples_engineering, examples_marketing

        examples_engineering.append({
            "inputs": {"question": sample["question"]},
            "outputs": {"answer": sample["gold_answer_research"]}
        })

        examples_marketing.append({
            "inputs": {"question": sample["question"]},
            "outputs": {"answer": sample["gold_answer_marketing"]}
        })

        counter += 1
    
    return examples_engineering, examples_marketing


def get_or_create_dataset(client: Client, dataset_name: str, examples: List[Dict[str, Any]]):
    """Get or create a LangSmith dataset."""
    if client.has_dataset(dataset_name=dataset_name):
        logger.info(f"Dataset '{dataset_name}' already exists, loading existing dataset.")
        dataset = client.read_dataset(dataset_name=dataset_name)
    else:
        logger.info(f"Dataset '{dataset_name}' does not exist, creating it now.")
        dataset = client.create_dataset(dataset_name=dataset_name)
        client.create_examples(dataset_id=dataset.id, examples=examples)
    return dataset


def initialize_rag_system(config: ExperimentConfig, vdm: VectorDatabaseManager, cohere_api_key: str) -> RAGSystem:
    """Initialize a RAG system with the given configuration."""
    logger.info(f"Initializing {config.rag_type} RAG system with {config.retriever_type} retriever")
    
    use_cohere = config.rag_type == "cohere"
    use_mistral = config.rag_type == "mistral"
    
    return RAGSystem(
        vector_db_manager=vdm,
        engineering_template_path=config.templates["engineering"],
        marketing_template_path=config.templates["marketing"],
        cohere_api_key=cohere_api_key,
        use_mistral=use_mistral,
        use_cohere=use_cohere,
        mistral_model_name=SupportedGeneratorModels.MistralInstructV2.value,
        top_k=config.top_k,
        retriever_type=config.retriever_type,
        retriever_kwargs=config.retriever_kwargs,
    )


def create_target_function(rag_system: RAGSystem, team: Team):
    """Create a target function for evaluation with a specific rag system and team."""
    def target(inputs: dict) -> dict:
        question = inputs["question"]
        logger.info(f"Processing question: {question[:50]}...")
        answer = rag_system.invoke(team, question)
        retrieved_docs = rag_system.query_vectorstore(question)
        return {
            "answer": answer,
            "documents": retrieved_docs
        }
    return target


def run_evaluation(config: ExperimentConfig, cohere_api_key: str, use_ragas: bool = False, limit: int = 78) -> Dict[str, Any]:
    """Run a single evaluation with given experiment configuration."""
    import gc
    import torch
    
    # Initialize vector database manager
    vdm = VectorDatabaseManager(
        embedding_model_name=config.embedding_model,
        collection_name="myrag",
        chunk_size=config.chunk_size,
        chunk_overlap=config.chunk_overlap,
        in_memory=True,
        force_recreate=True,
    )
    
    # Hydrate vector database with data sources
    logger.info(f"Hydrating vector database with {len(data_sources)} data sources")
    vdm.hydrate(data_sources)
    
    # Initialize RAG system
    rag_system = initialize_rag_system(config, vdm, cohere_api_key)
    
    # Define team and dataset
    team = Team.Engineering if config.team_type == "engineering" else Team.Marketing
    
    # Create LangSmith client
    client = Client()
    
    # Load validation data
    examples_engineering, examples_marketing = load_validation_data(limit=limit)
    examples = examples_engineering if config.team_type == "engineering" else examples_marketing
    
    # Get or create dataset
    dataset_name = f"w267-rag-validation-{config.team_type}" if limit == 78 else f"w267-rag-validation-{config.team_type}-limit{limit}"
    dataset = get_or_create_dataset(client, dataset_name, examples)
    
    # Create target function
    target = create_target_function(rag_system, team)
    
    # Get experiment ID - no longer modifying based on evaluation type
    experiment_id = config.get_experiment_id()
    logger.info(f"Starting evaluation: {experiment_id}")
    
    start_time = time.time()
    result_data = {}
    
    try:
        # Build list of evaluators based on the flags - can include multiple types
        evaluators = []
        
        # Add standard evaluators - use by default if nothing else is specified or if explicitly requested
        if args.standard or (not args.ragas and not args.deepeval):
            logger.info("Adding standard evaluators")
            evaluators.extend([
                correctness, 
                groundedness, 
                relevance, 
                retrieval_relevance
            ])
        
        # Add RAGAS evaluators if requested
        if use_ragas:
            logger.info("Adding RAGAS evaluators")
            evaluators.extend([
                ragas_answer_accuracy, 
                ragas_context_relevance, 
                ragas_faithfulness, 
                ragas_response_relevancy
            ])
        
        # Add DeepEval evaluators if requested and available
        if args.deepeval and DEEPEVAL_AVAILABLE:
            # Check that OpenAI API key is available (required for DeepEval)
            if not os.getenv("OPENAI_API_KEY"):
                logger.error("OPENAI_API_KEY environment variable is required for DeepEval to work")
            else:
                logger.info("Adding DeepEval evaluators")
                evaluators.extend([
                    deepeval_faithfulness,
                    deepeval_geval
                ])
        
        # Add BERTScore evaluator if requested and available
        if args.bertscore and BERTSCORE_AVAILABLE:
            logger.info("Adding BERTScore evaluator")
            evaluators.append(bertscore_evaluator)
        
        # If no evaluators were added, fall back to standard evaluators
        if not evaluators:
            logger.warning("No evaluators selected. Falling back to standard evaluators.")
            evaluators = [
                correctness, 
                groundedness, 
                relevance, 
                retrieval_relevance
            ]
        
        result = client.evaluate(
            target,
            data=dataset_name,
            evaluators=evaluators,
            experiment_prefix=experiment_id,
            metadata=rag_system.get_config(),
        )
        
        elapsed_time = time.time() - start_time
        logger.info(f"Evaluation completed in {elapsed_time:.1f} seconds: {experiment_id}")
        
        # Convert result to pandas dataframe to compute summary statistics
        result_df = result.to_pandas()
        
        # Compute summary statistics
        feedback_metrics = {}
        
        # Process feedback metrics
        feedback_cols = [col for col in result_df.columns if col.startswith('feedback.')]
        for col in feedback_cols:
            metric_name = col.replace('feedback.', '')
            values = result_df[col].dropna()
            
            if len(values) == 0:
                continue
                
            feedback_metrics[metric_name] = {
                'mean': float(values.mean()),
                'median': float(values.median()),
                'std': float(values.std()),
                'min': float(values.min()),
                'max': float(values.max()),
                'count': int(len(values))
            }
        
        # Compare output answers with reference answers
        text_stats = {}
        if 'outputs.answer' in result_df.columns and 'reference.answer' in result_df.columns:
            # Character length comparison
            output_char_lens = result_df['outputs.answer'].fillna('').astype(str).apply(len)
            reference_char_lens = result_df['reference.answer'].fillna('').astype(str).apply(len)
            char_ratios = output_char_lens / reference_char_lens.replace(0, float('nan'))
            
            # Word count comparison (simple word tokenization)
            output_word_counts = result_df['outputs.answer'].fillna('').astype(str).apply(
                lambda x: len(re.findall(r'\b\w+\b', x.lower()))
            )
            reference_word_counts = result_df['reference.answer'].fillna('').astype(str).apply(
                lambda x: len(re.findall(r'\b\w+\b', x.lower()))
            )
            word_ratios = output_word_counts / reference_word_counts.replace(0, float('nan'))
            
            text_stats = {
                'character_length': {
                    'mean_output': float(output_char_lens.mean()),
                    'mean_reference': float(reference_char_lens.mean()),
                    'mean_ratio': float(char_ratios.mean())
                },
                'word_count': {
                    'mean_output': float(output_word_counts.mean()),
                    'mean_reference': float(reference_word_counts.mean()),
                    'mean_ratio': float(word_ratios.mean())
                }
            }
            
            # Calculate TF-IDF similarity if there are enough examples
            if len(result_df) >= 2:
                try:
                    from sklearn.feature_extraction.text import TfidfVectorizer
                    from sklearn.metrics.pairwise import cosine_similarity
                    
                    outputs = result_df['outputs.answer'].fillna('').astype(str).tolist()
                    references = result_df['reference.answer'].fillna('').astype(str).tolist()
                    
                    vectorizer = TfidfVectorizer()
                    all_docs = outputs + references
                    tfidf_matrix = vectorizer.fit_transform(all_docs)
                    
                    n = len(outputs)
                    similarities = []
                    for i in range(n):
                        if i < len(tfidf_matrix) and i + n < len(tfidf_matrix):
                            output_vector = tfidf_matrix[i]
                            reference_vector = tfidf_matrix[i + n]
                            similarity = cosine_similarity(output_vector, reference_vector)[0][0]
                            similarities.append(similarity)
                    
                    if similarities:
                        text_stats['tfidf_similarity'] = {
                            'mean': float(np.mean(similarities)),
                            'min': float(np.min(similarities)),
                            'max': float(np.max(similarities))
                        }
                except Exception as e:
                    logger.warning(f"Could not compute TF-IDF similarity: {e}")
        
        result_data = {
            "experiment_id": experiment_id,
            "config": config.to_dict(),
            "success": True,
            "elapsed_time": elapsed_time,
            "evaluation_type": "ragas" if use_ragas else "original",
            "metrics": {
                "feedback": feedback_metrics,
                "text_comparison": text_stats
            }
        }
    
    except Exception as e:
        logger.error(f"Error in evaluation {experiment_id}: {e}", exc_info=True)
        
        result_data = {
            "experiment_id": experiment_id,
            "config": config.to_dict(),
            "success": False,
            "error": str(e),
            "evaluation_type": "ragas" if use_ragas else "original",
            "metrics": {}  # Empty metrics object on error
        }
    
    # Clean up to prevent memory leaks
    if config.rag_type == "mistral" and hasattr(rag_system, "llm") and hasattr(rag_system.llm, "pipeline"):
        logger.info(f"Cleaning up Mistral model for {experiment_id}")
        if hasattr(rag_system.llm.pipeline, "model"):
            # Delete the model to free GPU memory
            del rag_system.llm.pipeline.model
            
        # Delete the pipeline
        del rag_system.llm.pipeline
        
    # Delete the RAG system and vector DB manager
    del rag_system
    del vdm
    
    # Force garbage collection
    gc.collect()
    
    # Clean up RAGAS embedding model if using RAGAS evaluators
    if use_ragas:
        try:
            from src.rag267.evals.ragas import embedding_model
            embedding_model.clear_embeddings()
            logger.info("Cleaned up RAGAS embedding model")
        except Exception as e:
            logger.warning(f"Failed to clear RAGAS embedding model: {e}")
    
    # If using CUDA, clear CUDA cache
    if torch.cuda.is_available():
        logger.info("Clearing CUDA cache")
        torch.cuda.empty_cache()
        
    return result_data


def run_experiment_phase(
    phase: int, 
    max_parallel: int = 2,
    best_params: Optional[Dict[str, Any]] = None,
    output_dir: Optional[str] = None,
    use_ragas: bool = False,
    limit: int = 78
) -> Dict[str, Any]:
    """Run experiments for a specific phase"""
    
    # Get API keys
    cohere_api_key = os.getenv("COHERE_API_KEY_PROD")
    if not cohere_api_key:
        logger.error("Error: COHERE_API_KEY_PROD environment variable not set")
        raise ValueError("COHERE_API_KEY_PROD environment variable is required")
    
    # Create timestamp-based directory if not provided
    if not output_dir:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        eval_type = "ragas" if use_ragas else "standard"
        output_dir = f"results/phase{phase}_{eval_type}_{timestamp}"
    
    os.makedirs(output_dir, exist_ok=True)
    
    # Create experiments based on phase and best parameters from previous phases
    if phase == 0:
        experiments = create_test_experiments()
    elif phase == 1:
        experiments = create_phase1_experiments()
    elif phase == 2:
        if not best_params or 'embedding_model' not in best_params:
            raise ValueError("Phase 2 requires 'embedding_model' from Phase 1 results")
        experiments = create_phase2_experiments(best_params['embedding_model'])
    elif phase == 3:
        if not best_params or not all(k in best_params for k in ['embedding_model', 'chunk_size', 'chunk_overlap']):
            raise ValueError("Phase 3 requires 'embedding_model', 'chunk_size', and 'chunk_overlap' from Phase 2 results")
        experiments = create_phase3_experiments(
            best_params['embedding_model'], 
            best_params['chunk_size'], 
            best_params['chunk_overlap']
        )
    else:
        raise ValueError(f"Invalid phase: {phase}. Must be 0, 1, 2, or 3.")
    
    # Save the experiment plan
    with open(f"{output_dir}/experiment_plan.json", "w") as f:
        json.dump([config.to_dict() for config in experiments], f, indent=4)
    
    # Split experiments by model type
    cohere_experiments = [exp for exp in experiments if exp.rag_type == "cohere"]
    mistral_experiments = [exp for exp in experiments if exp.rag_type == "mistral"]
    
    logger.info(f"Running {len(experiments)} experiments for Phase {phase}")
    logger.info(f"Running {len(cohere_experiments)} Cohere experiments with parallelism {max_parallel}")
    logger.info(f"Running {len(mistral_experiments)} Mistral experiments sequentially")
    
    results = []
    
    # Process Cohere experiments in parallel
    if cohere_experiments:
        logger.info(f"Starting Cohere experiments with {max_parallel} workers")
        with ThreadPoolExecutor(max_workers=max_parallel) as executor:
            # Submit all Cohere experiments
            futures = {executor.submit(run_evaluation, config, cohere_api_key, use_ragas, limit): config 
                      for config in cohere_experiments}
            
            # Process results as they complete
            for future in tqdm(as_completed(futures), 
                              total=len(futures), 
                              desc="Cohere experiments"):
                config = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    logger.error(f"Error in Cohere experiment {config.get_experiment_id()}: {e}", exc_info=True)
                    results.append({
                        "experiment_id": config.get_experiment_id(),
                        "config": config.to_dict(),
                        "success": False,
                        "error": str(e),
                        "evaluation_type": "ragas" if use_ragas else "original",
                        "metrics": {}  # Empty metrics object on error
                    })
                
                # Save intermediate results after each experiment
                with open(f"{output_dir}/results.json", "w") as f:
                    json.dump(results, f, indent=4)
    
    # Process Mistral experiments sequentially
    if mistral_experiments:
        logger.info("Starting Mistral experiments (sequential execution)")
        for config in tqdm(mistral_experiments, desc="Mistral experiments"):
            try:
                result = run_evaluation(config, cohere_api_key, use_ragas)
                results.append(result)
            except Exception as e:
                logger.error(f"Error in Mistral experiment {config.get_experiment_id()}: {e}", exc_info=True)
                results.append({
                    "experiment_id": config.get_experiment_id(),
                    "config": config.to_dict(),
                    "success": False,
                    "error": str(e),
                    "evaluation_type": "ragas" if use_ragas else "original",
                    "metrics": {}  # Empty metrics object on error
                })
            
            # Save intermediate results after each experiment
            with open(f"{output_dir}/results.json", "w") as f:
                json.dump(results, f, indent=4)
    
    # Process results to find best configurations
    successful_results = [r for r in results if r.get("success", False)]
    
    if not successful_results:
        logger.error("No successful experiments to analyze")
        return {"phase": phase, "results": results, "output_dir": output_dir, "success": False}
    
    logger.info(f"Completed {len(successful_results)}/{len(experiments)} experiments successfully")
    logger.info(f"Results saved to {output_dir}/results.json")
    
    return {
        "phase": phase,
        "results": results,
        "output_dir": output_dir,
        "success": True
    }


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Run RAG experiment suite")
    parser.add_argument("--phase", type=int, required=True, choices=[0, 1, 2, 3],
                       help="Experiment phase to run (0, 1, 2, or 3)")
    parser.add_argument("--max_parallel", type=int, default=2, 
                       help="Maximum number of parallel experiments (default: 2)")
    parser.add_argument("--output_dir", type=str, default=None,
                       help="Directory to save results (default: auto-generated)")
    
    # Parameters for Phase 2 and 3
    parser.add_argument("--embedding_model", type=str, default=None,
                       help="Best embedding model from Phase 1 (required for Phase 2 and 3)")
    
    # Parameters for Phase 3
    parser.add_argument("--chunk_size", type=int, default=None,
                       help="Best chunk size from Phase 2 (required for Phase 3)")
    parser.add_argument("--chunk_overlap", type=int, default=None,
                       help="Best chunk overlap from Phase 2 (required for Phase 3)")
    
    # Evaluation selector
    parser.add_argument("--ragas", action="store_true",
                       help="Use Ragas evaluations (answer_accuracy, context_relevance, faithfulness, response_relevancy)")
    parser.add_argument("--deepeval", action="store_true",
                       help="Use DeepEval evaluations (faithfulness, geval) - requires OpenAI API key")
    parser.add_argument("--bertscore", action="store_true",
                       help="Use BERTScore evaluation to measure semantic similarity with reference answers")
    parser.add_argument("--standard", action="store_true", 
                       help="Use standard evaluations (will be used by default if no other evaluators are specified)")
    
    # Dataset limiting
    parser.add_argument("--limit", type=int, default=78,
                       help="Limit the number of questions used in evaluation (default: 78, which uses all questions)")
    
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_arguments()
    
    # Collect best parameters from previous phases based on command line arguments
    best_params = {}
    
    if args.phase >= 2:
        if not args.embedding_model:
            print("Error: --embedding_model is required for Phase 2 and 3")
            sys.exit(1)
        best_params['embedding_model'] = args.embedding_model
    
    if args.phase >= 3:
        if not args.chunk_size or not args.chunk_overlap:
            print("Error: --chunk_size and --chunk_overlap are required for Phase 3")
            sys.exit(1)
        best_params['chunk_size'] = args.chunk_size
        best_params['chunk_overlap'] = args.chunk_overlap
    
    # Log which evaluation sets we're using
    evaluators_used = []
    
    # Standard evaluations - use by default if nothing else is specified or if explicitly requested
    if args.standard or (not args.ragas and not args.deepeval and not args.bertscore):
        logger.info("Using standard evaluations: correctness, groundedness, relevance, retrieval_relevance")
        evaluators_used.append("standard")
    
    # RAGAS evaluations
    if args.ragas:
        logger.info("Using Ragas evaluations: answer_accuracy, context_relevance, faithfulness, response_relevancy")
        evaluators_used.append("ragas")
    
    # DeepEval evaluations
    if args.deepeval and DEEPEVAL_AVAILABLE:
        if not os.getenv("OPENAI_API_KEY"):
            logger.error("OPENAI_API_KEY environment variable is required for DeepEval evaluations")
        else:
            logger.info("Using DeepEval evaluations: faithfulness, geval")
            evaluators_used.append("deepeval")
            
    # BERTScore evaluations
    if args.bertscore and BERTSCORE_AVAILABLE:
        logger.info("Using BERTScore evaluation for semantic similarity measurement")
        evaluators_used.append("bertscore")
    
    if not evaluators_used:
        logger.warning("No evaluators selected. Falling back to standard evaluations.")
        evaluators_used.append("standard")
        
    # Log the question limit
    if args.limit < 78:
        logger.info(f"Using subset of validation data: {args.limit} questions (out of 78 total)")
    else:
        logger.info("Using full validation dataset: 78 questions")
    
    try:
        # Run the requested phase
        phase_results = run_experiment_phase(
            args.phase,
            args.max_parallel,
            best_params,
            args.output_dir,
            args.ragas,
            args.limit
        )
        
        if phase_results["success"]:
            print(f"\nPhase {args.phase} completed successfully!")
            print(f"Results saved to: {phase_results['output_dir']}")
            print("\nTo analyze results and determine the best configuration, examine the metrics in the results.json file.")
            
            evaluator_type = "ragas" if args.ragas else "standard"
            
            if args.phase == 1:
                print("\nAfter analyzing results, run Phase 2 with:")
                print(f"python run_experiment_suite.py --phase 2 --embedding_model <best_model_from_phase1> {'--ragas' if args.ragas else ''}")
            elif args.phase == 2:
                print("\nAfter analyzing results, run Phase 3 with:")
                print(f"python run_experiment_suite.py --phase 3 --embedding_model <best_model> --chunk_size <best_size> --chunk_overlap <best_overlap> {'--ragas' if args.ragas else ''}")
            
            sys.exit(0)
        else:
            print(f"\nPhase {args.phase} failed to complete successfully.")
            print(f"Check the logs and results in: {phase_results['output_dir']}")
            sys.exit(1)
            
    except Exception as e:
        print(f"Error running Phase {args.phase}: {str(e)}")
        sys.exit(1)