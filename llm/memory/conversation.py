class ConversationMemory:
    def __init__(self):
        self.sessions = {}
        self.interactionIDs = {}



    def get_history(self, sessionID):
        if sessionID not in self.sessions:
            self.sessions[sessionID] = []

        return self.sessions[sessionID]



    def add_message(
        self,
        sessionID,
        role,
        content,
    ):
        history = self.get_history(sessionID)

        history.append(
            {
                "role": role,
                "content": content,
            }
        )

    def setInteractionID(self, sessionID, interactionID):
        self.interactionIDs[sessionID] = interactionID

    def getLastID(self, sessionID):
        return self.interactionIDs.get(
            sessionID,
            None
        )