#!/usr/bin/env python3
import os
import argparse

def print_tree(directory, file_output=True, indent=''):
    for filename in os.listdir(directory):
        path = os.path.join(directory, filename)

        if os.path.isfile(path):
            print(indent + '├── ' + filename)
            if file_output:
                try:
                    with open(path, 'r', errors='ignore') as f:
                        for line in f:
                            print(indent + '│   ' + line.rstrip())
                    print(indent + '│   ')  # empty line after the end of each file's contents
                except Exception as e:
                    print(indent + '│   ERROR: ' + str(e)) 
        elif os.path.isdir(path):
            print(indent + '├── ' + filename + '/')
            print_tree(path, file_output, indent + '│   ')
    print(indent)


def main():
    parser = argparse.ArgumentParser(description='Display directory tree with file contents.')
    parser.add_argument('directory', type=str, nargs='?', default='.', help='Directory to start from')
    parser.add_argument('--no-file-output', action='store_true', help='Do not print file contents')

    args = parser.parse_args()

    print_tree(args.directory, not args.no_file_output)


if __name__ == "__main__":
    main()
