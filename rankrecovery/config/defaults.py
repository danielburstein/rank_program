"""Default configuration values.

Everything here is a *default*. The live values are stored in the database
settings store and edited from the GUI; these constants seed the store on first
run and back the "Restore Defaults" action.
"""

from __future__ import annotations

# Ordered lowest to highest. Priority = index + 1.
DEFAULT_PLAYER_RANKS: list[dict] = [
    {"name": "DEFAULT", "aliases": ["MEMBER"], "enabled": True},
    {"name": "HERO", "aliases": ["NOVA", "GLOBAL"], "enabled": True},
    {"name": "LEGEND", "aliases": [], "enabled": True},
    {"name": "TITAN", "aliases": [], "enabled": True},
    {"name": "GOD", "aliases": [], "enabled": True},
    {"name": "IMMORTAL", "aliases": ["SOLAR"], "enabled": True},
    {"name": "IMMORTAL+", "aliases": ["COSMIC"], "enabled": True},
    {"name": "GALACTIC", "aliases": [], "enabled": True},
    {"name": "INFINITE", "aliases": [], "enabled": True},
    {"name": "SCUFFED", "aliases": [], "enabled": True},
]

# Canonical staff designation -> raw spellings that mean the same designation.
DEFAULT_STAFF_PREFIXES: list[dict] = [
    {"name": "TRAINEE", "aliases": []},
    {"name": "HELPER", "aliases": []},
    {"name": "MOD", "aliases": ["MODERATOR"]},
    {"name": "SR MOD", "aliases": ["SRMOD", "SR. MOD", "SENIOR MOD"]},
    {"name": "HEAD MOD", "aliases": ["HEADMOD"]},
    {"name": "ADMIN", "aliases": ["ADMINISTRATOR"]},
    {"name": "ADMIN+", "aliases": []},
    {"name": "CO OWNER", "aliases": ["COOWNER", "CO-OWNER"]},
    {"name": "OWNER", "aliases": []},
    {"name": "DEV", "aliases": ["DEVELOPER"]},
]

# Old canonical rank -> new LuckPerms group. DEFAULT is deliberately unmapped.
DEFAULT_RANK_MAPPING: dict[str, str] = {
    "HERO": "thug",
    "LEGEND": "thug",
    "TITAN": "thug",
    "GOD": "bandit",
    "IMMORTAL": "bandit",
    "IMMORTAL+": "rogue",
    "GALACTIC": "assassin",
    "INFINITE": "assassin",
    "SCUFFED": "Warlord",
}

# Rank above which independent-submitter verification applies (strictly higher than).
DEFAULT_VERIFICATION_THRESHOLD_RANK = "IMMORTAL"
DEFAULT_REQUIRED_SOURCES_ABOVE_THRESHOLD = 2

# Known AI generator identifiers looked for in metadata values (case-insensitive substrings).
DEFAULT_AI_METADATA_INDICATORS: list[str] = [
    "chatgpt",
    "openai",
    "dall-e",
    "dall·e",
    "dalle",
    "gpt-image",
    "midjourney",
    "stable diffusion",
    "stablediffusion",
    "sdxl",
    "comfyui",
    "automatic1111",
    "invokeai",
    "novelai",
    "leonardo.ai",
    "firefly",
    "adobe firefly",
    "imagen",
    "gemini",
    "bing image creator",
    "designer.microsoft",
    "ideogram",
    "flux.1",
    "black forest labs",
    "trainedalgorithmicmedia",
    "digitalsourcetype",
    "c2pa",
    "ai-generated",
    "ai generated",
]

# Filename regexes (case-insensitive) that suggest AI tools. Weak evidence only.
DEFAULT_AI_FILENAME_PATTERNS: list[str] = [
    r"^dall[-_. ]?e",
    r"^chatgpt",
    r"^openai",
    r"^midjourney",
    r"^mj_",
    r"^sd_?\d",
    r"^comfyui",
    r"^imagen",
    r"^gemini_generated",
    r"^firefly",
    r"^leonardo",
    r"^ideogram",
    r"^img[-_]?\d+[-_]?(sd|ai)\b",
    r"\bgenerated[-_ ]?image\b",
]

DEFAULT_CUTOFF_DATE = "2026-01-01"

DEFAULT_SETTINGS: dict[str, object] = {
    # --- rank policy ---
    "ranks.player_ranks": DEFAULT_PLAYER_RANKS,
    "ranks.staff_prefixes": DEFAULT_STAFF_PREFIXES,
    "ranks.mapping": DEFAULT_RANK_MAPPING,
    "ranks.scuffed_rank": "SCUFFED",
    "ranks.allow_custom_prefix": True,
    # --- verification ---
    "verification.require_two_submitters_above_immortal": False,  # one eligible source confirms any rank; turn on for the stricter rule
    "verification.threshold_rank": DEFAULT_VERIFICATION_THRESHOLD_RANK,
    "verification.required_sources_above_threshold": DEFAULT_REQUIRED_SOURCES_ABOVE_THRESHOLD,
    "verification.per_rank_thresholds": {},  # e.g. {"SCUFFED": 3}
    "verification.unknown_submitter_counts_as_one_source": True,
    # --- trusted CSV ---
    "csv.treat_as_confirmed_exempt_from_verification": True,
    # --- AI metadata ---
    "ai.metadata_indicators": DEFAULT_AI_METADATA_INDICATORS,
    "ai.filename_patterns": DEFAULT_AI_FILENAME_PATTERNS,
    "ai.inspect_filenames": True,
    "ai.filename_indicator_makes_ineligible": False,
    # --- dates ---
    "dates.cutoff": DEFAULT_CUTOFF_DATE,
    "dates.trust_filename_pattern": True,
    "dates.trust_file_modified_time": False,
    "dates.exclude_out_of_date_from_eligibility": True,
    # --- extraction ---
    "extraction.default_extractor": "auto",  # auto | vision | ocr
    "extraction.vision_model": "claude-opus-5-5",
    "extraction.vision_effort": "medium",
    "extraction.vision_max_tokens": 16000,
    "extraction.vision_upscale_factor": 2,
    "extraction.vision_max_crop_edge": 1568,
    "extraction.vision_timeout_seconds": 120,
    "extraction.vision_max_retries": 3,
    "extraction.vision_concurrency": 3,
    "extraction.vision_model_layout": True,
    "extraction.vision_server_fallback": True,
    "extraction.vision_prompt_version": "1",
    "extraction.ocr_engine": "tesseract",
    "extraction.ocr_upscale_factor": 3,
    "extraction.ocr_workers": 2,
    "extraction.min_confidence": 0.6,
    "extraction.review_confidence": 0.8,
    "extraction.cache_results": True,
    # --- images ---
    "images.store_bytes_in_database": True,
    "images.phash_duplicate_distance": 6,
    "images.phash_near_duplicate_distance": 12,
    "images.max_animated_frames": 8,
    # --- lookup ---
    "lookup.java_provider": "mojang",
    "lookup.mojang_name_url": "https://api.mojang.com/users/profiles/minecraft/{name}",
    "lookup.minecraftservices_name_url": "https://api.minecraftservices.com/minecraft/profile/lookup/name/{name}",
    "lookup.bedrock_provider": "geyser",
    "lookup.geyser_url": "https://api.geysermc.org/v2/utils/uuid/bedrock_or_java/{name}?prefix={prefix}",
    "lookup.bedrock_prefix": ".",
    "lookup.historical_provider": "none",  # none | labynet | custom
    "lookup.labynet_previous_accounts_url": "https://laby.net/api/search/get-previous-accounts/{name}",
    "lookup.labynet_requests_per_minute": 30,
    "lookup.labynet_user_agent": "Mozilla/5.0 (compatible; RankRecovery/0.1; +https://github.com/danielburstein/rank_program)",
    "lookup.labynet_extra_headers": {},  # headers for an access token laby.net issued to you, e.g. {"X-Api-Token": "..."}
    "lookup.mojang_profile_url": "https://sessionserver.mojang.com/session/minecraft/profile/{name}",
    "lookup.profile_requests_per_minute": 60,
    "lookup.historical_url": "",
    "lookup.historical_uuid_path": "uuid",
    "lookup.historical_name_path": "name",
    "lookup.historical_not_found_statuses": [404],
    "lookup.workers": 4,
    "lookup.connect_timeout_seconds": 10,
    "lookup.read_timeout_seconds": 15,
    "lookup.max_retries": 3,
    "lookup.backoff_base_seconds": 1.0,
    "lookup.backoff_max_seconds": 30.0,
    "lookup.requests_per_minute": 100,
    "lookup.cache_ttl_hours": 24 * 7,
    "lookup.proxy_mode": "direct",  # direct | rotate | sticky
    "lookup.proxy_failure_threshold": 3,
    # --- conversion / export ---
    "conversion.input_mode": "mode1",
    "conversion.allowed_flags": [],
    "conversion.allow_resolved_flags": False,
    "conversion.presets": {},
    "export.command_template": "lp user {uuid} parent set {group}",
    "export.default_filename": "luckperms_commands.txt",
    # --- database ---
    "database.backup_dir": "backups",
    # --- GUI ---
    "gui.theme": "dark",
    "gui.page_size": 200,
    # --- schema ---
    "config.version": 1,
}

SECRET_SETTINGS: frozenset[str] = frozenset({"vision.anthropic_api_key", "lookup.proxy_list"})
