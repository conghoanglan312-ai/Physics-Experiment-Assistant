from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
import chromadb
from llm.app.geminiEmbedding import GeminiEmbeddingFunction

def ingest():
    loader = PyPDFLoader(
        r"llm\pdf\instructionBook.pdf"
    )

    documents = loader.load()

    print(
        "Pages:",
        len(documents)
    )


    splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=300
    )

    chunks = splitter.split_documents(
        documents
    )

    print(
        "Chunks:",
        len(chunks)
    )


    embedding_function = GeminiEmbeddingFunction()

    client = chromadb.PersistentClient(
        path="llm/chroma_db"
    )

    try: 
        client.delete_collection(
            "physics_manual"
        )
    except Exception as e:
        print(f"{e}, creating a new one")

    collection = client.create_collection(
        name="physics_manual",
        embedding_function=embedding_function
    )

    texts = []
    ids = []
    metadata = []


    for i, chunk in enumerate(chunks):
        texts.append(
            chunk.page_content
        )
        ids.append(
            f"chunk_{i}"
        )
        metadata.append(
            {
                "page": chunk.metadata.get("page"),
                "source": "instructionBook.pdf"
            }
        )

    collection.add(
        documents=texts,
        ids=ids,
        metadatas=metadata
    )

    print("Total documents in Chroma:", collection.count())
    print("Embedding completed!")

if __name__ == "__main__":
    ingest()