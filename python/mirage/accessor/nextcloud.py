from mirage.accessor.base import Accessor


class NextcloudAccessor(Accessor):

    def __init__(self, config) -> None:
        self.config = config
