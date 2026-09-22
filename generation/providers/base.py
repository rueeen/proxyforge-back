class ImageProvider:
    name: str

    def generate(self, prompt: str) -> bytes:
        """Devuelve los bytes de la imagen generada (PNG o JPEG)."""
        raise NotImplementedError
