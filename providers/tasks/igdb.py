"""Provider tasks"""
import os
import json
from datetime import datetime
from collections import defaultdict

import requests
from django.conf import settings
from django.core.files.base import ContentFile
from django.utils.timezone import make_aware

from celery import task
from celery.utils.log import get_task_logger
from common.util import slugify
from games.models import Game
from platforms.models import Platform
from providers.igdb import IGDBClient, GAME_CATEGORIES
from providers.models import Provider, ProviderGame, ProviderGenre, ProviderPlatform, ProviderCover
from common.models import save_action_log

LOGGER = get_task_logger(__name__)


IGDB_CAT = {
    "main_game": 0,
    "dlc_addon": 1,
    "expansion": 2,
    "bundle": 3,
    "standalone_expansion": 4,
    "mod": 5,
    "episode": 6,
    "season": 7,
    "remake": 8,
    "remaster": 9,
    "expanded_game": 10,
    "port": 11,
    "fork": 12
}


def _igdb_loader(resource_name, model):
    """Generic function to load a collection from IGDB to database"""
    client = IGDBClient(settings.TWITCH_CLIENT_ID, settings.TWITCH_CLIENT_SECRET)
    client.get_authentication_token()
    if resource_name not in ("games", "covers"):
        client.page_size = 10  # Most endpoints don't seem to support large page sizes
    resources = True
    page = 1
    provider, _created = Provider.objects.get_or_create(name="igdb")
    while resources:
        LOGGER.info("Getting page %s of IGDB API", page)
        response = client.get_resources(f"{resource_name}/", page=page)
        try:
            resources = response.json()
        except json.JSONDecodeError:
            LOGGER.error("Failed to read JSON response: %s", response.text)
            continue
        for api_payload in resources:
            model.create_from_igdb_api(provider, api_payload)
        page += 1


@task
def load_igdb_games():
    """Load all games from IGDB"""
    start = datetime.now()
    save_action_log("igdb_load_games_started_at", str(start))
    _igdb_loader("games", ProviderGame)
    end = datetime.now()
    save_action_log("igdb_load_games_ended_at", str(end))
    duration = end - start
    save_action_log("igdb_load_games_duration", duration.seconds)


@task
def load_igdb_genres():
    """Load all genres from IGDB"""
    _igdb_loader("genres", ProviderGenre)


@task
def load_igdb_platforms():
    """Load all platforms from IGDB"""
    _igdb_loader("platforms", ProviderPlatform)


@task
def load_igdb_covers():
    """Load all covers from IGDB"""
    _igdb_loader("covers", ProviderCover)


@task
def match_igdb_games():
    """Create or update Lutris games from IGDB games"""
    platforms = {
        p.igdb_id: p for p in Platform.objects.filter(igdb_id__isnull=False)
    }
    for igdb_game in ProviderGame.objects.filter(provider__name="igdb"):
        igdb_slug = igdb_game.metadata["slug"]
        # Only match main games
        if igdb_game.metadata["category"] != 0:
            continue
        if igdb_game.games.count():
            # Game is already matched
            continue
        if not igdb_slug:
            LOGGER.error("Missing slug for %s", igdb_game.metadata)
            continue
        try:
            lutris_game = Game.objects.get(slug=igdb_slug)
            LOGGER.info("Updating Lutris game %s", igdb_game.name)
        except Game.DoesNotExist:
            LOGGER.info("Creating Lutris game %s", igdb_game.name)
            lutris_game = Game.objects.create(
                name=igdb_game.name,
                slug=igdb_slug,
            )
        if not lutris_game.year and igdb_game.metadata.get("first_release_date"):
            lutris_game.year = datetime.fromtimestamp(igdb_game.metadata["first_release_date"]).year

        if not lutris_game.description and igdb_game.metadata.get("summary"):
            lutris_game.description = igdb_game.metadata.get("summary")
        for platform_id in igdb_game.metadata.get("platforms", []):
            try:
                lutris_game.platforms.add(platforms[platform_id])
            except KeyError:
                LOGGER.warning("No platform with ID %s", platform_id)
                continue
        lutris_game.provider_games.add(igdb_game)
        lutris_game.is_public = True
        lutris_game.save()


@task
def sync_igdb_platforms():
    """Syncs IGDB platforms to Lutris"""
    for igdb_platform in ProviderPlatform.objects.filter(provider__name="igdb"):
        igdb_slug = igdb_platform.metadata["slug"]
        try:
            lutris_platform = Platform.objects.get(slug=igdb_slug)
        except Platform.DoesNotExist:
            lutris_platform = Platform.objects.create(
                name=igdb_platform.name,
                slug=igdb_slug
            )
        lutris_platform.igdb_id = igdb_platform.metadata["id"]
        lutris_platform.save()


def get_igdb_cover(image_id, size="cover_big"):
    """Download a cover from IGDB and return its contents"""
    url = f"https://images.igdb.com/igdb/image/upload/t_{size}/{image_id}.jpg"
    response = requests.get(url)
    return response.content


@task
def sync_igdb_coverart(force_update=False):
    """Downloads IGDB coverart and associates it with Lutris games

    force_update redownloads coverarts for every game even if one is already present.
    """
    cover_format = "cover_big"
    for igdb_cover in ProviderCover.objects.filter(provider__name="igdb"):
        relpath = f"{cover_format}/{igdb_cover.image_id}.jpg"
        igdb_path = os.path.join(settings.MEDIA_ROOT, "igdb", relpath)
        if os.path.exists(igdb_path):
            continue
        try:
            igdb_game = ProviderGame.objects.get(provider__name="igdb", internal_id=igdb_cover.game)
        except ProviderGame.DoesNotExist:
            continue
        except ProviderGame.MultipleObjectsReturned:
            LOGGER.warning("Multiple games for %s", igdb_cover.game)
            igdb_game = ProviderGame.objects.filter(provider__name="igdb", internal_id=igdb_cover.game).first()
        try:
            lutris_game = Game.objects.get(provider_games=igdb_game)
        except Game.DoesNotExist:
            LOGGER.warning("No Lutris game with ID %s", igdb_cover.game)
            continue
        if lutris_game.coverart and not force_update:
            continue
        lutris_game.coverart = ContentFile(
            get_igdb_cover(igdb_cover.image_id),
            relpath
        )
        lutris_game.save()
        LOGGER.info("Saved cover for %s", lutris_game)


@task
def deduplicate_lutris_games():
    """IGDB uses a different slugify method,
    using dashes on apostrophes where we don't"""
    # Select all title with an apostrophe that don't have an IGDB game already
    games = Game.objects.filter(
        change_for__isnull=True,
        name__contains="'"
    ).exclude(provider_games__provider__name="igdb")
    for game in games:
        # Generate a new slug
        igdb_slug = slugify(game.name.replace("'", "-"))
        # Check the presence of an IGDB game
        try:
            igdb_game = Game.objects.get(provider_games__provider__name="igdb", slug=igdb_slug)
        except Game.DoesNotExist:
            # No IGDB game found, just keep going
            continue
        game.merge_with_game(igdb_game)


def deduplicate_igdb_games():
    """One time migration to put data from slug based entries
    onto ID based ones
    Slug based entries are then deleted.
    Once the process is finished, run fix_igdb_games.
    """
    for game_by_id in ProviderGame.objects.filter(provider__name="igdb"):
        game_id = game_by_id.metadata["id"]
        game_slug = game_by_id.metadata["slug"]
        if game_by_id.slug == game_slug:
            # Skip slug based entries
            continue
        try:
            game_by_slug = ProviderGame.objects.get(provider__name="igdb", slug=game_slug)
        except ProviderGame.DoesNotExist:
            # No older entry to attach the data to, skip.
            continue
        game_by_slug.metadata = game_by_id.metadata
        for game in game_by_id.games.all():
            game_by_slug.games.add(game)
        game_by_id.delete()
        game_by_slug.slug = game_slug
        game_by_slug.internal_id = game_id
        game_by_slug.save()


def fix_igdb_games():
    """One time migration process to populate the internal ID field"""
    for game in ProviderGame.objects.filter(provider__name="igdb"):
        game.slug = game.metadata["slug"]
        game.internal_id = game.metadata["id"]
        game.updated_at = make_aware(datetime.fromtimestamp(game.metadata["updated_at"]))
        game.save()


def remove_dlcs_from_games():
    """One time migration that removes Lutris games that have been created from IGDB games"""

    stats = defaultdict(int)
    for game in ProviderGame.objects.filter(provider__name="igdb"):
        stats["total"] += 1
        # Skip main games
        if game.metadata["category"] == IGDB_CAT["main_game"]:
            stats["skipped"] += 1
            continue
        # Filter by DLC and bundles only for now.
        if game.metadata["category"] not in (IGDB_CAT["dlc_addon"], IGDB_CAT["bundle"], IGDB_CAT["expansion"]):
            stats["not_dlc"] += 1
            continue

        # No Lutris game is associated
        if not game.games.all():
            stats["unlinked"] += 1
            continue

        lutris_game = game.games.first()

        # Keep games with installers
        if lutris_game.installers.all():
            stats["with_installer"] += 1
            continue

        # Keep games that have been added to user libraries
        if lutris_game.user_count > 0:
            stats["user_owned"] += 1
            continue

        lutris_game.delete()
        stats["removed"] += 1
    return stats
