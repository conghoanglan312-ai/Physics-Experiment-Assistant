import chromadb
from llm.app.geminiEmbedding import GeminiEmbeddingFunction

def contextRetrieval(userQuery):
    embedding_function = GeminiEmbeddingFunction()

    client = chromadb.PersistentClient(
        path="llm/chroma_db"
    )

    
    collection = client.get_collection(
        name="physics_manual",
        embedding_function=embedding_function
    )

    result = collection.query(
        query_texts=[
            userQuery
        ],
        n_results=3
    )

    text = """"""

    for i, doc in enumerate(result["documents"][0]):
        text += f"Chunk {i+1}\n"
        text += doc
        text += "\n"

    return text