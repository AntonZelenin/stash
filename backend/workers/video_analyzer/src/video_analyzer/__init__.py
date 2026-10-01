"""Video analyzer: consumes `VIDEO_ANALYSIS_JOBS` (published by the API for
uploaded videos), samples frames across each video with ffmpeg, describes
them together via OpenAI as one description of the whole video, and
completes the item."""
