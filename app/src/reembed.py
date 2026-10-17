"""Re-embed every chunk in the collection: python -m src.reembed"""

from src.main import COLLECTION, reembed_collection

if __name__ == "__main__":
    print(f"Re-embedded {reembed_collection()} chunks in {COLLECTION}")
