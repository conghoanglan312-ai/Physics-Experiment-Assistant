from llm.AI_model.gemini import GeminiLLM
from llm.AI_model.zai import ZaiLLM

from .app.timeoutLLM import TimeoutLLM
from .app.userQueryLimiter import UserQueryLimiter
from .AI_model.base import LLMEvent
from .memory.conversation import ConversationMemory
from .util.generateSystemPrompt import generateSystemPromptFromUserQuery
from .util.generateModelMessage import *
from uuid import uuid4

memory = ConversationMemory()


def validated_query_or_error(user_query):
    return UserQueryLimiter(user_query).validate()


def _session_id(session_id):
    """Use an isolated session when a caller does not supply one."""
    return session_id if session_id is not None else str(uuid4())

def getSafeDropletData(sessionID):
    from cv.api import sample, llm_context, live_status

    memory.updateDropletSessionData(sessionID, live_status())

    data = memory.getDropletSessionData(sessionID)
    safe_data = llm_context(data)

    return safe_data

def getResponseFromGemini(userQuery, sessionID = None):
    valid, error = validated_query_or_error(userQuery)
    if not valid:
        yield LLMEvent(type="error", data=error)
        return

    sessionID = _session_id(sessionID)

    # Check if session is active or not 
    if (memory.isSessionActive(sessionID)): 
        yield generateErrorMessage("More than one input detected. Please wait for the current response to finish before sending another query.")
        return
    
    memory.startSession(sessionID)

    try:
        safeDropletData = getSafeDropletData(sessionID)
        system_prompt = generateSystemPromptFromUserQuery(userQuery, safeDropletData)
        model = TimeoutLLM(GeminiLLM())

        # Get Previous ID used for stateful conversation
        previousID = memory.getLastInteractionID(sessionID)

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
            elif event.type == "done" or event.type == "error":
                memory.terminateSession(sessionID)

            yield event

        memory.addUserMessage(sessionID, userQuery)
        memory.addAssistantMessage(sessionID, chatBotResponse)
    finally:
        # Runs no matter how this generator exits: a normal finish, the
        # "done"/"error" event above (terminateSession() is idempotent, so
        # calling it twice is harmless), an exception raised anywhere in this
        # block -- getSafeDropletData(), prompt generation, GeminiLLM()
        # construction, or the first pull from model.stream(), which is
        # exactly where a missing or invalid API key surfaces -- or the
        # generator being closed early by a caller that stopped iterating.
        #
        # Before this, startSession() above had no matching release on any
        # of those paths: a raised exception skips straight past the
        # "done"/"error" check inside the loop, leaves hasActiveSession
        # stuck True for this sessionID, and every later request for the
        # same session -- the frontend always reuses one fixed sessionID --
        # is rejected with "More than one input detected" forever, even
        # after the underlying problem (e.g. the API key) is fixed.
        memory.terminateSession(sessionID)


def getResponseFromZai(userQuery, sessionID = None):
    valid, error = validated_query_or_error(userQuery)
    if not valid:
        yield LLMEvent(type="error", data=error)
        return

    sessionID = _session_id(sessionID)

    # Check if session is active or not
    if (memory.isSessionActive(sessionID)): 
        yield generateErrorMessage("More than one input detected. Please wait for the current response to finish before sending another query.")
        return
        
    memory.startSession(sessionID)
    
    try:
        safeDropletData = getSafeDropletData(sessionID)
        system_prompt = generateSystemPromptFromUserQuery(userQuery, safeDropletData)

        # The provider receives the current system prompt separately from
        # conversation history. Do not persist it in user conversation memory.
        memory.addUserMessage(sessionID, userQuery)

        # Initialize the model (ZhiPu AI)
        model = TimeoutLLM(ZaiLLM())

        # Fetch History from memory used for stateful conversation
        history = memory.getInteractionHistory(sessionID)

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
            elif event.type == "done" or event.type == "error":
                memory.terminateSession(sessionID)

            yield event

        memory.addAssistantMessage(sessionID, chatBotResponse)
    finally:
        # Same guarantee as getResponseFromGemini above: this runs on every
        # exit, so a construction failure (ZaiLLM()), a prompt-generation
        # error, or a raw exception from model.stream() cannot leave
        # hasActiveSession stuck True for this sessionID.
        memory.terminateSession(sessionID)

if __name__ == "__main__":
    queries = [
    # Basic Concept Questions
    "What is the purpose of the Millikan Oil Drop Experiment?",
    "Explain the principle behind the Millikan Oil Drop Experiment.",
    "Who performed the Millikan Oil Drop Experiment and what did it discover?",
    "Why are oil droplets used instead of water droplets in the experiment?",
    "What physical quantity is measured in the Millikan Oil Drop Experiment?"]

    for userQuery in queries:
        print("\nUser Input: ", userQuery)

        print("Chat Bot Response: ")
        stream = getResponseFromGemini(userQuery)

        for event in stream:
            if (event.type == "text"):
                print(event.data, end = '')
