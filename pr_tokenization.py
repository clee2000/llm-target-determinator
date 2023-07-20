from typing import Any

from transformers import AutoTokenizer


class PTTokenizer:
    def __init__(self, model_checkpoint: str = "bigcode/starcoderplus"):
        self.model_checkpoint = model_checkpoint
        self.tokenizer = AutoTokenizer.from_pretrained(
            self.model_checkpoint, use_auth_token=True
        )
        self.tokenizer.pad_token = "[PAD]"

    def encode(self, data: Any) -> Any:
        print(data)
        return self.tokenizer.encode(data, return_tensors="pt", padding=True)

    def decode(self, tokenized_data: Any) -> str:
        return self.tokenizer.decode(tokenized_data)
