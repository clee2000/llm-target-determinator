import json
import os
import sys
import time
from datetime import datetime

import torch

from test_dataset import collate_fn, UnittestDataset
from pr_tokenization import CONTEXT_LENGTH
from torch.utils.data import DataLoader, DistributedSampler

from transformers import AutoModelForCausalLM
from llama import Llama


class Indexer:
    def __init__(self):
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
        x = torch.rand(1)
        print(x.device)

        # Create DataLoader
        dataset = UnittestDataset("assets/filelist.json")
        sampler = DistributedSampler(
            dataset, num_replicas=self.world_size, rank=self.local_rank,
        )
        self.dataloader = DataLoader(
            dataset, collate_fn=collate_fn, batch_size=2, sampler=sampler,
        )
        print("init dataloader done")

        # Load Model
        # self.model = AutoModelForCausalLM.from_pretrained(
        #     "codellama/CodeLlama-7b-Python-hf"
        # ).to(self.device)
        # TODO: make these cmd line args
        self.generator = Llama.build(
            ckpt_dir="/home/osalpekar/codellama/CodeLlama-7b-Python/",
            tokenizer_path="/home/osalpekar/codellama/CodeLlama-7b-Python/tokenizer.model",
            max_seq_len=CONTEXT_LENGTH,
            max_batch_size=600,
            use_kv_cache=False,
            model_parallel_size=1,
        )

    def index(self):
        embeddings = []
        function_list = []

        # self.model.eval()

        with torch.no_grad():
            for idx, batch in enumerate(self.dataloader, 0):
                print(idx)
                inputs, functions = batch
                if inputs.shape[0] == 0:
                    continue
                inputs = inputs.to(self.device)

                # TODO: make tokenizer handle pad_id
                # full_model_states = self.model(
                #     inputs, output_hidden_states=True
                # )
                _, embedding = self.generator.model.forward(inputs, 0, output_last_hidden_state=True)
                del inputs

                # Embedding is (num_functions x context_length x 4096)
                # embedding = full_model_states.hidden_states[-1].detach()

                # Pooled Embedding is (num_functions x 4096)
                pooled_embedding = torch.sum(embedding, dim=1)
                del embedding

                embedding_cpu = pooled_embedding.to("cpu")
                del pooled_embedding
                embeddings.append(embedding_cpu)
                function_list.extend(functions)

                # if idx == 1:
                #     break

        embeddings = torch.cat(embeddings)
        print(embeddings.shape)
        # self.save_index(embeddings, function_list)

    def save_index(self, embeddings, function_list):
        rand = hash(datetime.now()) & sys.maxsize
        torch.save(
            embeddings, f"assets/unittest_index_{rand}_{self.local_rank}.pt"
        )

        with open(
            f"assets/unittest_index_mapping_{rand}_{self.local_rank}.json", "w+"
        ) as f:
            json.dump({"mapping": function_list}, f)


if __name__ == "__main__":
    start = time.time()
    indexer = Indexer()
    indexer.index()
    end = time.time()

    print(f"Total time to generate embeddings: {end-start} seconds")
