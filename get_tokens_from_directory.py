import argparse
import ast
import multiprocessing
import os
import pprint
import re
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import pr_tokenization
import torch
import json

from cache_data import TensorCache
from typing import Optional

MAX_TOKENS = 8292


def get_function_text_from_file(filename: str, selected_lines: List[Tuple[int, int]] = []) -> Dict[str, str]:
    def _is_in_scope(begin_lineno, end_lineno, selected_lines) -> bool:
        if not selected_lines:
            return True

        overlapping = False
        for scope in selected_lines:
            selected_begin = scope[0]
            selected_end = scope[1]

            if end_lineno < selected_begin or begin_lineno > selected_end:
                continue

            overlapping = True

        return overlapping

    with open(filename, "r") as file:
        content = file.read()

    module = ast.parse(content)

    functions = {}

    for node in ast.walk(module):
        if isinstance(node, ast.FunctionDef):
            # If the node is a function, extract its name and its arguments
            signature = node.name
            if not _is_in_scope(node.lineno, node.end_lineno, selected_lines):
                continue
            body = ast.get_source_segment(content, node)
            functions[signature] = body
        elif isinstance(node, ast.ClassDef):
            # If the node is a class, we also want to get its methods
            for sub_node in node.body:
                if isinstance(sub_node, ast.FunctionDef) and _is_in_scope(sub_node.lineno, sub_node.end_lineno, selected_lines):
                    signature = node.name + "." + sub_node.name
                    body = ast.get_source_segment(content, sub_node)
                    functions[signature] = body

    return functions


def extract_tokens_from_text(text: str):
    tokenizer = pr_tokenization.PTTokenizer()
    tokens = tokenizer.encode(text)
    return tokens


def extract_text_from_file(filename):
    with open(filename, "r") as file:
        text = file.read()
    return text


def get_tokens_from_file(file_path: Path, repo_dir: Path, tests_only: bool = False, selected_lines: List[Tuple[int, int]] = []):
    """
    root_dir is generally the repository root, so that you can use the cached data
    """
    print(f"Parsing {file_path}")

    all_file_tokens = defaultdict(list)

    cache = None
    if repo_dir:
        cache = TensorCache(Path("cache"), "tokens_from_file")
        relative_file_path = Path(str(file_path).replace(str(repo_dir), ""))

    if cache and cache.get_cache_data(relative_file_path):
        print(f"Cache hit for {relative_file_path}")
        return cache.get_cache_data(relative_file_path)

    functions = get_function_text_from_file(file_path, selected_lines)
    print(f"Found {len(functions.items())} functions")
    for function_name, text in functions.items():
        # Skip unless the function_name matches the regex r/.*\.test_.*/
        if (
            tests_only
            and not function_name.startswith("test")
            and not re.match(r".*\.test_.*", function_name)
        ):
            print(f"Skipping {function_name} since it's not a test function")
            continue

        print(f"Extracting tokens from {function_name}")
        tokens = extract_tokens_from_text(text)
        print(f"Got {tokens.shape[1]} tokens")
        if tokens.shape[1] >= MAX_TOKENS:
            # split tokens into chunks of MAX_TOKENS
            tokens = torch.split(tokens, MAX_TOKENS, dim=1)
        else:
            tokens = [tokens]
        all_file_tokens[str(file_path) + ":" + function_name] = tokens

    # Save file to cache
    cache.save_cache_data(relative_file_path, all_file_tokens) if cache else None

    return all_file_tokens

def write_token_dict_as_json(token_dict, filename):
    writable_dict = {key: [token.tolist() for token in tokens] for key, tokens in token_dict.items()}
    with open(filename, 'w') as f:
        f.write(json.dumps(writable_dict))

def get_effective_directory(repo_dir, directory):
    if repo_dir:
        if not repo_dir.is_absolute():
            raise Exception(f"repo_dir {repo_dir} must be an absolute path")

        if directory.is_absolute():
            # Ensure directory is a subdirectory of repo_dir
            if not str(directory).startswith(repo_dir):
                raise Exception(
                    f"Directory {directory} is not a subdirectory of {repo_dir}"
                )
        else:
            if str(directory).startswith("~"):
                raise Exception(
                    f"Don't use '~' in your path. Directory {directory} must be a subdirectory of {repo_dir}"
                )
            else:
                directory = repo_dir / directory

    else:
        # Really, we should block not using the repo_dir.
        # But leaving this route open for testing
        print("No repo_dir provided. Won't be using cache")

    print(f"Directory: {directory}. Exists: {directory.exists()}")

    return directory

def should_process_file(file: str, root: str, desired_file_prefix: str):
    return file.endswith(".py") and file.startswith(desired_file_prefix) and "third_party" not in root

def get_tokens_from_directory(
    directory: Path, repo_dir: Path = None, file_prefix="", tests_only=True, output_file: Optional[str] = None
):
    """
    directory: Should be inside repo_dir if you want to use the cache (can be relative to repo_dir, e.g. repo_dir="~/pytorch", directory="test")
    repo_dir: Path to repository. Required if you want to use the cache
    file_prefix: If set, only files that start with this prefix will be parsed
    output_file: If set, the tokens will be written to this file. If set to None (default), will not write to file
    """

    directory = get_effective_directory(repo_dir, directory)

    all_tokens = defaultdict(list)
    for root, dirs, files in os.walk(directory):
        for file in files:
            if should_process_file(file, root, file_prefix):
                file_path = os.path.join(root, file)
                file_tokens = get_tokens_from_file(
                    file_path=file_path, repo_dir=repo_dir, tests_only=tests_only
                )
                all_tokens.update(file_tokens)
                print(f"Done parsing {file_path}")
    if output_file:
        write_token_dict_as_json(all_tokens, output_file)
    return all_tokens

def process_file(file, repo_dir, tests_only):
    return get_tokens_from_file(file_path=file, repo_dir=repo_dir, tests_only=tests_only)

def get_tokens_from_directory_with_multiprocessing(directory: Path, repo_dir: Path = None, file_prefix="", tests_only=True, output_file: Optional[str] = None):
    directory = get_effective_directory(repo_dir, directory)

    all_files = []
    for root, dirs, files in os.walk(directory):
        for file in files:
            if should_process_file(file, root, file_prefix):
                file_path = os.path.join(root, file)
                all_files.append(file_path)

    process_file_args = [(file, repo_dir, tests_only) for file in all_files]
    pool = multiprocessing.Pool()
    results = pool.starmap(process_file, process_file_args)

    all_tokens = defaultdict(list)
    for file_tokens in results:
        all_tokens.update(file_tokens)
    if output_file:
        # dump as json
        write_token_dict_as_json(all_tokens, output_file)
    return all_tokens

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default="")
    parser.add_argument("--repo_dir", type=Path, default="~/pytorch")
    parser.add_argument("--file_prefix", type=str, default="test_")
    parser.add_argument("--output_file", type=str, default=None)
    parser.add_argument("--use_multiprocessing", type=bool, default=True)
    args = parser.parse_args()
    use_multiprocessing = args.use_multiprocessing
    if use_multiprocessing:
        tokens = get_tokens_from_directory_with_multiprocessing(args.directory, repo_dir=args.repo_dir, file_prefix=args.file_prefix, output_file=args.output_file)
    else:
        tokens = get_tokens_from_directory(args.directory, repo_dir=args.repo_dir, file_prefix=args.file_prefix, output_file=args.output_file)
    pprint.pprint(tokens)
