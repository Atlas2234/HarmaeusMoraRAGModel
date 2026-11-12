# Script for chunking JSONL files into smaller pieces for processing
import json
import re

# Import necessary modules for text splitting and chunking
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)

from pathlib import Path

# Import random module for any randomization needs
import random

# Folder this script lives in, and the project root one level up
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent

# Constants
MAX_CHUNK_SIZE = 1000
DATA_PATH = ROOT / "data/raw/Lore.jsonl"
OUT_PATH = ROOT / "data/Chunked/chunks.json"
HEADING_LEVELS = ("h1", "h2", "h3")
MIN_CHUNK_SIZE = 150
RANDOM_SEED = 42
RANDOM_SAMPLE = True
SKIP_SECTIONS = {"references", "gallery", "see also"} # Sections to skip when processing chunks because they are considered low value
SEPARATOR = "\n\n"
TAIL_SIZE = 50              # tiny tails may merge even if the result reaches MAX_MERGED_SIZE
MAX_MERGED_SIZE = 1200
SENTENCE_END = (".", "!", "?", '"', "”", ":", ")")

# Initialize text splitters for chunking
header_splitter = MarkdownHeaderTextSplitter(
    headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3")]# Split on markdown headers
)
# Initialize size-based splitter for chunking
size_splitter = RecursiveCharacterTextSplitter(chunk_size=MAX_CHUNK_SIZE, chunk_overlap=0) 

# Function to remove duplicate chunks based on their text content
# This helps to avoid processing the same text multiple times
def remove_duplicates(chunks):
    seen = set()
    unique = []
    for chunk in chunks:
        if chunk["text"] not in seen:
            seen.add(chunk["text"])
            unique.append(chunk)
    print(f"Removed {len(chunks) - len(unique)} duplicate chunks")
    return unique

def choose_separator(previous_text):
    """Space if the previous text was cut mid-sentence, paragraph break otherwise."""
    if previous_text.rstrip().endswith(SENTENCE_END):
        return SEPARATOR
    return " "

# Merge small chunks into larger ones if they are below the minimum chunk size
def absorb_small(pieces):
    """Merge fragments into the previous piece of the same page."""
    merged = []
    for piece in pieces:
        text = piece.page_content.strip() # Remove leading and trailing whitespace from the chunk's content
        if not text: # Skip empty chunks after stripping whitespace
            continue
        if merged: # Check if there is a previous chunk to potentially merge with
            prev = merged[-1]
            separator = choose_separator(prev.page_content) # Determine the appropriate separator based on the previous text's ending
            total = len(prev.page_content) + len(separator) + len(text) # Total length if merged with previous chunk
            same_section = piece.metadata == prev.metadata # Check if the current chunk is in the same section as the previous one
            small = same_section and len(text) < MIN_CHUNK_SIZE and total <= MAX_CHUNK_SIZE # Check if the current chunk is small and can be merged with the previous one
            tail = same_section and len(text) < TAIL_SIZE and total <= MAX_MERGED_SIZE # Check if the current chunk is a tail piece that can be merged with the previous one
            if small or tail: # Merge the current chunk with the previous one if it is small or a tail piece
                prev.page_content += separator + text
                continue
        piece.page_content = text # Update the current chunk's content with the stripped text
        merged.append(piece)
    return merged

def is_low_value_section(path):
    """True if any heading below the page title is one we don't want.

    Example: "Si'ess > See Also > Books" -> True, because "See Also" is skipped.
    """
    headings = path.split(" > ")

    # The first heading is the page title, so only look at the sections below it
    section_headings = headings[1:]

    # Iterate over each section heading below the page title to check if it is in the set of low-value sections
    for heading in section_headings:
        name = heading.strip().lower()
        if name in SKIP_SECTIONS: # If the heading is in the set of low-value sections, mark this path as low value
            return True

    return False # No low-value sections found in the path

# Function to determine if a section is considered low value based on its heading path
def remove_low_value_pieces(pieces):
    """Drop pieces that sit under sections like References, Gallery, or See Also."""
    kept = []
    for piece in pieces:
        section_path = heading_path(piece.metadata)
        if is_low_value_section(section_path): # Skip sections that are considered low value
            continue
        kept.append(piece)
    return kept

# Function to generate a heading path string from the metadata of a chunk
def heading_path(metadata):
    """Turn {'h1': 'Dragonborn', 'h2': 'Powers'} into 'Dragonborn > Powers'."""

    names = []

    # Collect the names of the headings in the order of HEADING_LEVELS if they exist in the metadata
    for level in HEADING_LEVELS:
        if level in metadata:
            names.append(metadata[level])
    return " > ".join(names)

# Function to chunk text into smaller pieces based on sections and paragraphs
def chunk_text(title, text, chunks):
    # Stop table rows like "# of Zones | 1" from being read as headings
    text = re.sub(r"^#+\s*(?=[^\n]*\|)", "", text, flags=re.MULTILINE)

    # Put heading markers that got glued to the previous sentence back on their own line
    text = re.sub(r"(?<=[^\n#])(#{2,6} )(?=\S)", r"\n\n\1", text)

    # Split the page at its markdown headings.
    sections = header_splitter.split_text(text)

    # Remove sections that are considered low value based on their heading path
    sections = remove_low_value_pieces(sections)

    # Split any section that is still too long and merge small chunks into larger ones
    pieces = absorb_small(size_splitter.split_documents(sections)) # Merge small chunks into larger ones if possible

    # Turn each piece into a chunk dictionary.
    for i, piece in enumerate(pieces):
        chunks.append({
            "id": f"{title}-{i}",
            "title": title,
            "heading": heading_path(piece.metadata) or title, # Use the title as a fallback if no heading is found
            "text": piece.page_content,
        })
     
def parse_jsonl(have_limit, limit):
    chunks = []
    line_count = 0
    
    with open(DATA_PATH, encoding="utf-8") as f: # Using encoding=utf-8 to handle special characters
        lines = f.readlines()

    if have_limit:
        parse_limit = limit
    else:
        parse_limit = len(lines)

    # If RANDOM_SAMPLE is enabled, shuffle the lines in the file to get a random sample
    if RANDOM_SAMPLE:
        random.seed(RANDOM_SEED)
        random.shuffle(lines)

    for line in lines:
        # Skip lines that exceed the maximum count
        if line_count >= parse_limit:
            break

        page = json.loads(line)

        # Increment the line count for each processed line
        line_count += 1

        # Add the chunks for the current page to the overall list of chunks
        chunk_text(page["title"], page["text"], chunks)

    chunks = remove_duplicates(chunks)
    return chunks

# Entry point for the script
def main():
    have_limit = False
    limit = 1000

    # Parse the Lore JSONL file with optional line limit
    chunks = parse_jsonl(have_limit, limit)

    # Save the chunks to the output file in JSON format
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(chunks, f, indent=2, ensure_ascii=False)
    print(f"Chunks saved to {OUT_PATH}")
    
if __name__ == "__main__":
    main()
    