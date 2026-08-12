from llm.AI_model.gemini import GeminiLLM
from llm.AI_model.zai import ZaiLLM

from .app.timeoutLLM import TimeoutLLM
from .memory.conversation import ConversationMemory
from .app.generateSystemPrompt import generateSystemPromptFromUserQuery

memory = ConversationMemory()

def getResponseFromGemini(userQuery, sessionID = None):
    system_prompt = generateSystemPromptFromUserQuery(userQuery)
    model = TimeoutLLM(GeminiLLM())

    previousID = memory.getLastID(sessionID)

    stream = model.stream(
        userQuery=userQuery, 
        previousID=previousID, 
        system_prompt=system_prompt,
        history=None
    )

    chatBotResponse = ""
    for event in stream:
        if event.type == "id":
            memory.setInteractionID(sessionID, event.data)
        elif event.type == "text":
            chatBotResponse += event.data

        yield event

    memory.add_message(sessionID, "user", userQuery)
    memory.add_message(sessionID, "assistant", chatBotResponse)
    print(memory.get_history(sessionID), "\n\n")


def getResponseFromZai(userQuery, sessionID = None):
    system_prompt = generateSystemPromptFromUserQuery(userQuery)

    memory.add_message(sessionID, "system", system_prompt)
    model = TimeoutLLM(ZaiLLM())

    memory.add_message(sessionID, "user", userQuery)
    history = memory.get_history(sessionID)
    print(history, '\n\n\n')
    stream = model.stream(
            userQuery=userQuery, 
            system_prompt=system_prompt,
            history=history
        )
    
    chatBotResponse = ""
    for event in stream:
        if event.type == "id":
            memory.setInteractionID(sessionID, event.data)
        elif event.type == "text":
            chatBotResponse += event.data
    
        yield event
    
    memory.add_message(sessionID, "assistant", chatBotResponse)
    print(memory.get_history(sessionID), "\n\n")

if __name__ == "__main__":
    queries = [
    # Basic Concept Questions
    "What is the purpose of the Millikan Oil Drop Experiment?",
    "Explain the principle behind the Millikan Oil Drop Experiment.",
    "Who performed the Millikan Oil Drop Experiment and what did it discover?",
    "Why are oil droplets used instead of water droplets in the experiment?",
    "What physical quantity is measured in the Millikan Oil Drop Experiment?"]

    for userQuery in queries:
        print("User Input: ", userQuery)

        print("Chat Bot Response: ")
        stream = getResponseFromGemini(userQuery)

        for event in stream:
            if (event.type == "text"):
                print(event.data, end = '')