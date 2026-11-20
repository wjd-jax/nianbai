"""Install Qwen3-TTS Base and VoiceDesign for private voices."""

from setup_models import prepare


if __name__ == "__main__":
    prepare(private=True)
