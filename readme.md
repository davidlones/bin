# Sol AI Utility

Sol is a powerful AI utility designed to assist in searching and analyzing data through a conversation with OpenAI's language model. It uses OpenAI's GPT model to generate responses to user queries and employs embeddings to find the most relevant context for user queries from text data.

## Prerequisites

To run this program, you'll need the following:

- Python 3.8+
- OpenAI's Python client library, `openai`
- `sklearn` Python library
- `tqdm` Python library
- `concurrent.futures` module for parallel execution
- `pickle` module for object serialization
- `dotenv` module to load environment variables from a .env file
- `argparse` for command-line argument parsing

## Setup

Before you can use Sol, you need to install the necessary Python libraries. You can do this with pip:

```bash
pip install openai sklearn tqdm python-dotenv argparse
```

You also need to set your OpenAI API key. You can do this by creating a `.env` file in the same directory as `sol.py` and setting your API key there:

```bash
echo "OPENAI_API_KEY=your-api-key" > .env
```

Replace `your-api-key` with your actual OpenAI API key.

## How to Use

To use Sol, you run it from the command line with your query as an argument:

```bash
python sol.py 'your query here'
```

You can provide additional flags to modify the behavior of Sol:

- `--show-history`: Print the conversation history.
- `--no-context`: Ask the question without providing any context.
- `--recursive`: Enable recursive search through directories. By default, the search is limited to the current directory.
- `--extensions`: A list of file extensions to include. For example, `--extensions .txt .md` would limit the search to text and markdown files.
- `--exclude`: A list of directories to exclude from the search. For example, `--exclude dir1 dir2` would exclude 'dir1' and 'dir2' from the search.
- `--file`: Provide a path to a text file to use as context.
- `--string`: Provide a string to use as context.
- `-v` or `--verbose`: Enable verbose output. By default, the output is limited to essential information.

## What It Does

When you run Sol, it does the following:

1. Loads conversation history.
2. If the `--show-history` flag is set, it prints the conversation history.
3. If there is no conversation history, it starts a new conversation with the system message.
4. Depending on the flags set and the arguments given, it generates a context for the user's question from a file, a string, or files in the current directory tree.
5. It adds the user's question to the messages.
6. It generates a completion using OpenAI's chat-based language model.
7. It prints the assistant's reply.
8. It saves the conversation history.

Note: If an error occurs while generating a completion, it catches the exception and prints an error message.

The Sol utility provides an easy way to search through text data and have a conversation with an AI assistant that is informed by that data.