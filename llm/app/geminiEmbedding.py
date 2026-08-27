from google import genai
from google.genai import types
import os
from dotenv import load_dotenv


load_dotenv()

class GeminiEmbeddingFunction:
    def __init__(self):
        self.client = genai.Client(
            api_key=os.getenv("GEMINI_API_KEY")
        )

    def name(self):
        return "gemini-embedding-001"

    # Used when adding documents
    def __call__(self, input):

        result = self.client.models.embed_content(
            model="gemini-embedding-001",
            contents=input,
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_DOCUMENT",
                output_dimensionality=768
            )
        )

        return [
            embedding.values
            for embedding in result.embeddings
        ]

    # Used when searching
    def embed_query(self, input):

        result = self.client.models.embed_content(
            model="gemini-embedding-001",
            contents=input,
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_QUERY",
                output_dimensionality=768
            )
        )

        return [
            result.embeddings[0].values
        ]