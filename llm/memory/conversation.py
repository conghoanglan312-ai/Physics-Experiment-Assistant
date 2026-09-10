from llm.config.settings import MAX_MESSAGES_PER_SESSION

class ConversationMemory:
    def __init__(self):
        self.sessions = {}
        self.interactionIDs = {}
        self.hasActiveSession = {}
        self.dropletSessionData={}

    def getInteractionHistory(self, sessionID):
        if sessionID not in self.sessions:
            self.sessions[sessionID] = []

        return self.sessions[sessionID]

    def addMessage(
        self,
        sessionID,
        role,
        content
    ):
        history = self.getInteractionHistory(sessionID)

        history.append(
            {
                "role": role,
                "content": content
            }
        )
        # Bound local history so repeated large prompts cannot grow provider
        # requests without limit. Preserve complete user/assistant exchanges.
        if len(history) > MAX_MESSAGES_PER_SESSION:
            del history[:2]

    def addUserMessage(
        self,
        sessionID,
        content,
    ):
        self.addMessage(sessionID, "user", content)

    def addSystemMessage(
        self,
        sessionID,
        content
    ):
        self.addMessage(sessionID, "system", content)

    def addAssistantMessage(
            self,
            sessionID,
            content
        ):
            self.addMessage(sessionID, "assistant", content)

    def setInteractionID(self, sessionID, interactionID):
        self.interactionIDs[sessionID] = interactionID

    def getLastInteractionID(self, sessionID):
        return self.interactionIDs.get(
            sessionID,
            None
        )

    def startSession(self, sessionID):
        self.hasActiveSession[sessionID] = True

    def terminateSession(self, sessionID):
        self.hasActiveSession[sessionID] = False

    def isSessionActive(self, sessionID):
        return self.hasActiveSession.get(
            sessionID,
            False
        )

    def updateDropletSessionData(self, sessionID, dropletData):
        self.dropletSessionData[sessionID] = dropletData

    def getDropletSessionData(self, sessionID):
        return self.dropletSessionData.get(
            sessionID,
            None
        )

    def deleteSession(self, sessionID):
        if sessionID in self.sessions:
            del self.sessions[sessionID]
        if sessionID in self.interactionIDs:
            del self.interactionIDs[sessionID]
        if sessionID in self.hasActiveSession:
            del self.hasActiveSession[sessionID]
        if sessionID in self.dropletSessionData:
            del self.dropletSessionData[sessionID]