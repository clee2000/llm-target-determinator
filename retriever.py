import json
import os
import time
from argparse import ArgumentParser
from glob import glob
from pathlib import Path
from typing import Dict, List, Any
from collections import defaultdict

import torch
import torch.nn.functional as F
from config import TDArgs

from llama import Llama

from preproc import get_functions
from tokenizer import Tokenizer
from transformers import AutoModelForCausalLM
from gen_pr_items import PR_ITEMS

REPO_ROOT = Path(__file__).resolve().parent


class EmbeddingGenerator:
    """
    Generate embedding for an item based using the model
    """

    def __init__(self):
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.config = TDArgs()

        generator = Llama.build(
            ckpt_dir=os.path.expanduser(self.config.model_ckpt_dir),
            tokenizer_path=os.path.expanduser(self.config.tokenizer_path),
            max_seq_len=self.config.max_context_len,
            max_batch_size=self.config.max_batch_size,
            use_kv_cache=False,
            model_parallel_size=1,
        )
        self.model = generator.model.to(self.device)
        self.tokenizer = Tokenizer(self.config)

    def gen_embedding(self, items: List[str]) -> List[Any]:
        # Tokenizes and generates embedding for each item.  Returns a list of
        # embeddings
        self.model.eval()
        with torch.autocast(
            self.device
        ):  # needed for cpu inference? something about half floats
            with torch.no_grad():
                embeddings = []
                for item in items:
                    tensor = torch.full(
                        (1, self.config.max_context_len),
                        self.tokenizer.pad_id,
                        dtype=torch.long,
                    )

                    tokens = self.tokenizer.encode(item)
                    tokens = tokens[: self.config.max_context_len]
                    tensor[0, : len(tokens)] = torch.tensor(
                        tokens, dtype=torch.long
                    )
                    attn_mask = torch.where(tensor == self.tokenizer.pad_id, 0.0, 1.0)

                    tensor = tensor.to(self.device)
                    attn_mask = attn_mask.to(self.device)

                    _, func_embedding = self.model.forward(
                        tensor, 0, output_last_hidden_state=True, attn_mask=attn_mask
                    )
                    pooled_embedding = torch.sum(func_embedding, dim=1)
                    embeddings.append(pooled_embedding)
                return embeddings


class Retriever:
    """Evaluate an embedding given an experiment"""

    def __init__(self, experiment_name):
        # Init Rank/Device
        try:
            self.local_rank = int(os.environ["LOCAL_RANK"])
            self.world_size = int(os.environ["WORLD_SIZE"])
        except KeyError:
            # LOCAL_RANK may not be set if torchrun/torchx is not being used
            self.local_rank = 0
            self.world_size = 1

        self.device = (
            torch.device(f"cuda:{self.local_rank}")
            if torch.cuda.is_available()
            else torch.device("cpu")
        )
        self.experiment_name = experiment_name
        assets_path = os.path.join("assets", self.experiment_name)

        # Get the list of artifacts:
        # 1. Indexes of unittests generated by indexer (*.pt)
        # 2. Mapping from indices to unittest names (*.json)
        embeddings_files = glob(f"{assets_path}/unittest_index_*.pt")
        mapping_files = glob(f"{assets_path}/unittest_index_mapping_*.json")

        # Sort the above lists
        embeddings_files = sorted(embeddings_files)
        mapping_files = sorted(mapping_files)

        # Read the artifact files and concatenate them into:
        # 1. self.embeddings - with the entire index as a single pytorch tensor
        # 2. self.unittest_names - a single Dict of the form {idx: test_name}
        embeddings = []
        self.unittest_names = []
        for i in range(len(embeddings_files)):
            embeddings.append(torch.load(embeddings_files[i]))

            with open(mapping_files[i]) as f:
                test_map = json.load(f)
            self.unittest_names.extend(test_map["mapping"])

        self.embeddings = torch.cat(embeddings).to(self.device)
        print(self.embeddings.shape)

    def evaluate_embedding(self, pooled_embeddings):
        # Takes the pooled embedding and evaluates it against the embeddings
        # from the indexer
        mapping = {}
        for embedding in pooled_embeddings:
            similarity_matrix = F.cosine_similarity(self.embeddings, embedding)

            grouped_by_file = defaultdict(list)

            for ind in range(similarity_matrix.shape[0]):
                test = self.unittest_names[ind]
                score = similarity_matrix[ind]
                if test not in mapping:
                    mapping[test] = []
                mapping[test].append(score.item())

        # condense
        for test, score in mapping.items():
            mapping[test] = sum(score) / len(score)
        return mapping


def gen_output_file(filename, mapping):
    """Make json file of the mapping in assets/mappings"""
    os.makedirs("assets/mappings", exist_ok=True)
    new_mapping = {}
    for file, score in mapping.items():
        clean_file = os.path.relpath(file, REPO_ROOT.parent / "pytorch/test")
        new_mapping[clean_file] = score
    with open(REPO_ROOT / "assets/mappings" / filename, "w") as f:
        f.write(json.dumps(new_mapping))
        print(f"Made output file assets/mappings/{filename}")


def main():
    parser = ArgumentParser("Retriever")
    parser.add_argument(
        "--experiment-names",
        nargs="+",
        required=True,
        help="Uses artifacts from the specified Indexer Experiments",
    )
    parser.add_argument(
        "--pr-items",
        nargs="+",
        choices=PR_ITEMS.keys(),
        required=True,
        help="Specify what method to parse information from a PR",
    )

    args = parser.parse_args()

    start = time.time()
    experiments = {
        experiment: Retriever(experiment)
        for experiment in args.experiment_names
    }
    print(f"Took {time.time() - start}s to load experiments")

    start = time.time()
    embedding_generator = EmbeddingGenerator()

    pr_items = {
        pr_item: embedding_generator.gen_embedding(PR_ITEMS[pr_item]())
        for pr_item in args.pr_items
    }
    print(f"Took {time.time() - start}s to generate embeddings")

    start = time.time()
    for experiment, retriever in experiments.items():
        for pr_item, embeddings in pr_items.items():
            mapping = retriever.evaluate_embedding(embeddings)
            gen_output_file(f"{experiment}_{pr_item}.json", mapping)
    print(f"Took {time.time() - start}s to evaluate embeddings")


if __name__ == "__main__":
    main()
