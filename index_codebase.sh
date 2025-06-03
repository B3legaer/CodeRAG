#!/bin/bash

# Load environment variables from .env file if it exists
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

# Use CODEBASE_PATH from .env as default, or command line argument if provided
if [ $# -eq 0 ]; then
    if [ -z "$CODEBASE_PATH" ]; then
        echo "Error: Please provide the folder path as an argument or set CODEBASE_PATH in .env file"
        echo "Usage: ./index_codebase.sh <folder_path>"
        exit 1
    fi
    folder_path="$CODEBASE_PATH"
else
    folder_path="$1"
fi

# Check if the folder exists
if [ ! -d "$folder_path" ]; then
    echo "Error: Directory '$folder_path' does not exist"
    exit 1
fi

echo "Processing the directory at $folder_path..."

# Run scripts with the folder_path
python preprocessing.py "$folder_path"
python create_tables.py "$folder_path"

echo "Processing complete."

echo "Please run python app.py <absolute_path_to_folder> to run the server"
