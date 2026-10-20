class ImageProvider:
    name: str

    def generate(self, prompt: str, aspect_ratio: str = "16:9", references=None, resolution=None) -> bytes:
        """Devuelve los bytes de la imagen generada (PNG o JPEG)."""
        raise NotImplementedError


def validate_aspect_ratio(aspect_ratio):
    if aspect_ratio not in ("16:9", "3:4", "2:3"):
        raise ValueError("Relación de aspecto no compatible")
