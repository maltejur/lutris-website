import yaml
SUCCESS = (True, "")


def validate_installer(installer):
    errors = []
    is_valid = True
    rules = [
        doesnt_contain_useless_fields,
        files_is_an_array,
        scummvm_has_gameid,
    ]
    for rule in rules:
        success, message = rule(installer)
        if not success:
            errors.append(message)
            is_valid = False

    return (is_valid, errors)


def doesnt_contain_useless_fields(installer):
    script = yaml.safe_load(installer.content)
    for field in (
        'version', 'gogid', 'humbleid', 'game_slug', 'description',
        'installer_slug', 'name', 'notes', 'runner', 'slug', 'steamid', 'year'
    ):
        if field in script:
            return (False, "Don't put a '{}' field in the script.".format(field))
    return SUCCESS


def files_is_an_array(installer):
    script = yaml.safe_load(installer.content)
    if 'files' in script:
        if not isinstance(script['files'], list):
            return (False, "'files' section should be an array.")
    return SUCCESS


def scummvm_has_gameid(installer):
    runner = installer.runner
    script = yaml.safe_load(installer.content)
    if runner.name != 'scummvm':
        return SUCCESS
    if 'game' not in script:
        return (
            False,
            "Missing section 'game'"
        )
    if 'game_id' not in script['game']:
        return (
            False,
            "ScummVM game should have a game identifier in the 'game' section"
        )
    return SUCCESS
