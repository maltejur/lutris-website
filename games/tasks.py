"""Celery tasks for account related jobs"""
from celery.utils.log import get_task_logger
from celery import task
from reversion.models import Version, Revision
from django.db.models import Q
from django.contrib.contenttypes.models import ContentType
from games import models
from common.models import KeyValueStore, save_action_log

LOGGER = get_task_logger(__name__)



@task
def delete_unchanged_forks():
    """Periodically delete forked installers that haven't received any changes"""
    installers_deleted = 0
    for installer in models.Installer.objects.abandoned():
        installer.delete()
        installers_deleted += 1
    save_action_log("delete_unchanged_forks", installers_deleted)


@task
def clear_orphan_versions():
    """Deletes versions that are no longer associated with an installer"""
    content_type = ContentType.objects.get_for_model(models.Installer)
    versions_deleted = 0
    for version in Version.objects.filter(content_type=content_type):
        if version.object:
            continue
        LOGGER.warning("Deleting orphan version %s", version)
        version.delete()
        versions_deleted += 1
    save_action_log("clear_orphan_versions", versions_deleted)


@task
def clear_orphan_revisions():
    """Clear revisions that are no longer attached to any object"""
    result = Revision.objects.filter(version__isnull=True).delete()
    save_action_log("clear_orphan_revisions", result[0])


@task
def clean_action_log():
    KeyValueStore.objects.filter(value=0).delete()


@task
def auto_process_installers():
    """Auto deletes or accepts some submissions"""
    revisions = Revision.objects.all()
    for revision in revisions:
        version = revision.version_set.first()
        if not version:
            continue
        try:
            submission = models.InstallerRevision(version)
        except Exception as ex:  # pylint: disable=broad-except
            LOGGER.error("Deleting corrupt submission %s: %s", version, ex)
            submission.delete()
            continue
        original = version.object
        if not original:
            LOGGER.warning("Could not find original, deleting %s", submission)
            submission.delete()
            continue
        if submission.content.strip() != original.content.strip():
            # Content change needs manual review
            continue

        if submission.runner != original.runner:
            if submission.runner.slug == "steam" and original.runner.slug == "winesteam":
                LOGGER.info("Accepting %s (%s > %s)",
                            submission, original.runner, submission.runner)
                submission.accept()
                continue
            LOGGER.info("Rejecting %s (%s > %s)",
                        submission, original.runner, submission.runner)
            submission.delete()
            continue
        if not submission.description:
            submission.description = ""
        if not original.description:
            original.description = ""
        if (
                submission.description.strip() == original.description.strip()
                and submission.notes.strip() == original.notes.strip()
        ):
            LOGGER.info("No change in submission, deleting %s", submission)
            submission.delete()
            continue

        if "[draft]" in revision.comment:
            LOGGER.info("Deleting draft  with only meta changes %s", submission)
            submission.delete()
            continue

        if original.version == "Change Me":
            LOGGER.info("Deleting garbage fork %s", submission)
            submission.delete()
            continue
