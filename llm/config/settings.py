MODELS = {
    "gemini": {
        "model" : "gemini-3.1-flash-lite",
        "stream" : True,
        "temperature": 0.7,
    },
    "zai": {
        "model" : "glm-5.2",
        "stream": True,
        "temperature": 0.7
    },
    "gemini-embedding": {
        "model" : "gemini-embedding-001",
        "output_dimensionality" : 768
    }
}

MAX_USER_INPUT_LENGTH = 2000
MAX_MESSAGES_PER_SESSION = 20