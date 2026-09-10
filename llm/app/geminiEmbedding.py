from google import genai
from google.genai import types
import os
from dotenv import load_dotenv
from llm.config.settings import MODELS

load_dotenv()
config = MODELS["gemini-embedding"]

class GeminiEmbeddingFunction:
    def __init__(self):
        self.client = genai.Client(
            api_key=os.getenv("GEMINI_API_KEY")
        )

    def name(self):
        return config["model"]

    # Used when adding documents
    def __call__(self, input):

        result = self.client.models.embed_content(
            model=config["model"],
            contents=input,
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_DOCUMENT",
                output_dimensionality=config["output_dimensionality"]
            )
        )

        return [
            embedding.values
            for embedding in result.embeddings
        ]

    # Used when searching
    def embed_query(self, input):

        result = self.client.models.embed_content(
            model=config["model"],
            contents=input,
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_QUERY",
                output_dimensionality=config["output_dimensionality"]
            )
        )

        return [
            result.embeddings[0].values
        ]