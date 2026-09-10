from llm.AI_model.base import LLMEvent

def generateErrorMessage(error):
    return LLMEvent(
        type = "error",
        data=str(error)
    )

def generateDoneMessage():
    return LLMEvent(
        type= "done"
    )

def generateTextMessage(text):
    return LLMEvent(
        type="text",
        data=text
    )

def generateIDMessage(id):
    return LLMEvent(
        type="id",
        data=id
    )