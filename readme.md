# Sol AI Utility with TreeCat

Sol is an advanced AI utility designed to facilitate searching and analyzing data through a conversation with OpenAI's language model. It uses OpenAI's GPT model to generate responses to user queries and employs embeddings to find the most relevant context for user queries from text data. This version of Sol integrates the TreeCat utility to visualize the directory structure and file contents that the AI is using to generate its responses.

## Prerequisites

For running this program, you will need the following:

- Python 3.8+
- OpenAI's Python client library, `openai`
- `sklearn` Python library
- `tqdm` Python library
- `concurrent.futures` for parallel execution
- `pickle` module for object serialization
- `dotenv` to load environment variables from a .env file
- `argparse` for command-line argument parsing

## Setup

Before you can utilize Sol, the necessary Python libraries need to be installed. This can be achieved using pip:

```bash
pip install openai sklearn tqdm python-dotenv argparse
```

Your OpenAI API key is required as well. This can be set by creating a `.env` file in the same directory as `sol.py` and setting your API key there:

```bash
echo "OPENAI_API_KEY=your-api-key" > .env
```

Please ensure to replace `your-api-key` with your actual OpenAI API key.

## Usage

Sol is used from the command line with your query as an argument:

```bash
python sol.py 'your query here'
```

Additional flags can be provided to modify Sol's behavior:

- `--show-history`: Prints the conversation history.
- `--no-context`: Asks the question without providing any context.
- `--recursive`: Enables recursive search through directories. By default, search is limited to the current directory.
- `--extensions`: A list of file extensions to include. For example, `--extensions .txt .md` would limit the search to text and markdown files.
- `--exclude`: A list of directories to exclude from the search. For example, `--exclude dir1 dir2` would exclude 'dir1' and 'dir2' from the search.
- `--file`: Provide a path to a text file to use as context.
- `--string`: Provide a string to use as context.
- `-v` or `--verbose`: Enables verbose output. By default, output is limited to essential information.

## Workflow

When you run Sol, it performs the following:

1. Loads conversation history.
2. If the `--show-history` flag is set, it prints the conversation history.
3. If there is no conversation history, it initiates a new conversation with the system message.
4. Depending on the flags set and the arguments given, it generates a context for the user's question from a file, a string, or files in the current directory tree.
5. It adds the user's question to the messages.
6. It generates a completion using OpenAI's chat-based language model.
7. It prints the assistant's reply.
8. It saves the conversation history.

## Utilizing TreeCat

TreeCat is a utility that is included in this package. It provides a tree-like visualization of a directory structure and can optionally print the contents of each file.

To use TreeCat, you run it from the command line and provide the directory you want to visualize:

```bash
python treecat.py /path/to/directory
```

You can also specify the `--no-file-output` flag to only print the directory structure and not the contents of the files:

```bash
python treecat.py /path/to/directory --no-file-output
```

This is useful if you want to get a quick overview of the directory structure that Sol is working with.

Note: If an error occurs while generating a completion, it catches the exception and prints an error message. The Sol utility with TreeCat offers an easy way to search through text data and have a conversation with an AI assistant that is informed by that data, with the added advantage of visualizing the file structure and contents.

If you run TreeCat on a directory using the command `python treecat.py project_directory`, the output could look something like this:

```bash
├── project_directory/
│   ├── main.py
│   │   # contents of main.py
│   │   print("Hello, World!")
│   │   
│   ├── helper.py
│   │   # contents of helper.py
│   │   def add(a, b):
│   │       return a + b
│   │   
│   ├── data/
│   │   ├── data1.txt
│   │   │   # contents of data1.txt
│   │   │   Hello, this is some text data.
│   │   │   
│   │   ├── data2.csv
│   │   │   # contents of data2.csv
│   │   │   id,name,score
│   │   │   1,Alice,85
│   │   │   2,Bob,90
│   │   │   
│   ├── docs/
│   │   ├── readme.md
│   │   │   # contents of readme.md
│   │   │   # Project Title
│   │   │   
│   │   ├── contributing.md
│   │   │   # contents of contributing.md
│   │   │   # How to Contribute
│   │   │   
```

In the output, `├──` represents a file or a directory, `│   ` is used to represent the hierarchical level of a file or a directory, and `# contents of ...` denotes the contents of a file.

Note: The actual contents of the files in the above output are just simple examples. Your actual files' contents could be much more complex and large.

If you do not wish to see the file contents, you can use the `--no-file-output` option:

```bash
python treecat.py project_directory --no-file-output
```

This would output only the directory and file structure, excluding the contents:

```bash
├── project_directory/
│   ├── main.py
│   ├── helper.py
│   ├── data/
│   │   ├── data1.txt
│   │   ├── data2.csv
│   ├── docs/
│   │   ├── readme.md
│   │   ├── contributing.md
```

For this hypothetical example, let's assume that we're interested in finding information related to the company's stock price performance in Q2 2022. So, the question we want to ask Sol could be "What was the trend in our company's stock prices in Q2 2022?".

To use Sol with this query, you would run it from the command line like this:

```bash
python sol.py "What was the trend in our company's stock prices in Q2 2022?" --recursive --extensions .txt .csv .json
```

The output would look something like this:

```bash
Scanning for file embeddings...
Generating embeddings: 100%|███████████████████████| 4/4 [00:02<00:00,  2.50it/s]
Starting conversation with OpenAI's GPT-3.5...

The trend in the company's stock prices in Q2 2022 was generally positive, with a noticeable uptick in May and June. The stock price saw a significant increase, particularly in the latter half of Q2. Various factors contributed to this positive trend, including successful product launches, strong sales data, and positive market sentiment. 

Please note that this is a summary based on available data and documents, and you should refer to the actual financial reports and data for more detailed information.
```

In this example, Sol.py would read through the text and CSV files, generate embeddings for each chunk of text, calculate the similarity of each embedding to the question, and then form a system message using the content from the most similar chunks. The question is then asked using these system messages as the initial conversation, and the assistant's reply is printed out.

Please note that this is a hypothetical example. The actual response will depend on the contents of your documents and the specifics of your question.