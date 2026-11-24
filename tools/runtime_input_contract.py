"""The signed manifest is shared by installation, runtime identity and Gradle inputs."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit

MANIFEST = 'app/src/main/assets/managed-runtime-inputs.json'
ASSETS = 'app/src/main/assets'
JAVA = 'app/src/main/java/com/deepseekharness/app'
BUILTIN_RUNTIME_SUFFIXES = {'.js', '.cjs', '.mjs', '.json', '.yml', '.yaml', '.css', '.wasm',
                            '.html', '.svg', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico',
                            '.woff', '.woff2', '.ttf', '.otf'}
# 过渡期第二个固定输入：hermes 运行时独立锁。锁不存在时整段跳过，dsh 路径保持原样。
HERMES_LOCK = 'ci/hermes-runtime.lock.json'
HERMES_ASSET = 'hermes-runtime.bin'
HERMES_META_ASSET = 'hermes-runtime.inputs.json'
HERMES_LOCK_FIELDS = ('asset', 'url', 'sha256', 'bytes', 'metaAsset', 'metaUrl', 'metaSha256',
                      'metaBytes', 'version', 'commit')


def relative(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value or '\0' in value \
            or value.startswith('/') or any(p in ('', '.', '..') for p in value.split('/')):
        raise ValueError('INVALID_RUNTIME_INPUT_PATH: ' + repr(value))
    return value


def load(root):
    root = Path(root)
    spec = json.loads((root / MANIFEST).read_text(encoding='utf-8'))
    if spec.get('schema') != 1:
        raise ValueError('RUNTIME_INPUT_SCHEMA')
    targets, assets = set(), set()
    for row in spec['installs']:
        source, target = relative(row['asset']), relative(row['target'])
        if source in assets or target in targets or not isinstance(row['executable'], bool):
            raise ValueError('DUPLICATE_OR_INVALID_RUNTIME_INSTALL')
        assets.add(source)
        targets.add(target)
    for key in ('assetFiles', 'assetTrees', 'launcherTrees', 'launcherSources'):
        values = spec[key]
        if not isinstance(values, list) or len(set(values)) != len(values):
            raise ValueError('RUNTIME_INPUT_LIST: ' + key)
        for value in values:
            relative(value)
    generators=spec.get('generatorSources', [])
    if not isinstance(generators,list) or len(set(generators))!=len(generators):
        raise ValueError('RUNTIME_GENERATOR_SOURCE_LIST')
    for value in generators:relative(value)
    verify_builtin_installs(root, spec)
    return spec


def _https(value):
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 or c.isspace() or c == '\\'
                                         for c in value):
        raise ValueError('HERMES_LOCK_URL')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None \
            or parsed.password is not None:
        raise ValueError('HERMES_LOCK_URL')


def hermes_lock(root):
    """可选 hermes 固定输入锁；文件不存在时返回 None，不报错、不影响 dsh。

    hermes 在过渡期只是第二个固定输入：它不进 managed-runtime-inputs.json，也不进
    描述符的 inputs（那些字节仍由 dsh 运行时身份覆盖），所以这里只做格式与定长校验。
    """
    path = Path(root) / HERMES_LOCK
    if not path.is_file():
        return None
    lock = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(lock, dict) or lock.get('schema') != 1 or lock.get('runtime') != 'hermes' \
            or lock.get('asset') != HERMES_ASSET or lock.get('metaAsset') != HERMES_META_ASSET:
        raise ValueError('HERMES_LOCK_SCHEMA')
    for key in ('sha256', 'metaSha256'):
        if not isinstance(lock.get(key), str) or not re.fullmatch(r'[a-f0-9]{64}', lock[key]):
            raise ValueError('HERMES_LOCK_DIGEST: ' + key)
    for key in ('bytes', 'metaBytes'):
        if type(lock.get(key)) is not int or not 0 < lock[key] <= 2 * 1024 ** 3:
            raise ValueError('HERMES_LOCK_BYTES: ' + key)
    for key in ('url', 'metaUrl'):
        _https(lock.get(key))
    for key in ('version', 'commit'):
        if not isinstance(lock.get(key), str) or not re.fullmatch(r'[A-Za-z0-9._+-]{1,80}', lock[key]):
            raise ValueError('HERMES_LOCK_IDENTITY: ' + key)
    return {key: lock[key] for key in HERMES_LOCK_FIELDS}


def _digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def hermes_section(root):
    """描述符的 hermes 段：锁存在时给出，本地归档存在时先核对字节。

    这不是 dsh 的身份字段（Java 侧 RuntimeDescriptor 只认固定字段表），因此
    过渡期不改 dshVersion / runtimeId；hermes 归档还没还原时只记录锁里的摘要，
    还原与字节校验由 tools/ci-assets.py --hermes 负责。
    """
    lock = hermes_lock(root)
    if lock is None:
        return None
    assets = Path(root) / ASSETS
    archive = assets / lock['asset']
    if archive.exists() or archive.is_symlink():
        if archive.is_symlink() or not archive.is_file() \
                or not archive.resolve().is_relative_to(assets.resolve()) \
                or archive.stat().st_size != lock['bytes'] or _digest(archive) != lock['sha256']:
            raise ValueError('HERMES_RUNTIME_MISMATCH: ' + lock['asset'])
    return {'asset': lock['asset'], 'sha256': lock['sha256'], 'bytes': lock['bytes'],
            'version': lock['version'], 'commit': lock['commit']}


def verify_builtin_installs(root, spec):
    """APK presence and a descriptor digest do not prove delivery to a guest entity.

    Every runtime file in each signed builtin must have its exact installer row.
    TypeScript source, declaration files, source maps and documentation stay outside
    this runtime rule; this never adds user/plugin paths to the managed boundary.
    """
    root = Path(root)
    registry = root / ASSETS / 'builtin-plugins.json'
    if not registry.is_file():
        return  # Small isolated descriptor fixtures need not contain the plugin registry.
    rows = {row['asset']: row for row in spec['installs']}
    for plugin in json.loads(registry.read_text(encoding='utf-8'))['plugins']:
        name, guest = plugin['name'], plugin['guestDirectory']
        tree = 'app-integration' if name == 'dsh-app-integration' else 'builtin-plugins/' + name
        directory = root / ASSETS / tree
        if not directory.is_dir():
            raise ValueError('BUILTIN_RUNTIME_TREE_MISSING:' + name)
        for path in directory.rglob('*'):
            suffix = path.relative_to(directory).as_posix()
            if not path.is_file() or suffix.startswith('src/') or path.suffix not in BUILTIN_RUNTIME_SUFFIXES:
                continue
            asset = tree + '/' + suffix
            expected = guest.lstrip('/') + '/' + suffix
            if asset not in rows:
                raise ValueError('BUILTIN_RUNTIME_FILE_UNINSTALLED:' + asset)
            if rows[asset]['target'] != expected:
                raise ValueError('BUILTIN_RUNTIME_TARGET_MISMATCH:' + asset)


def _file(root, relative_path):
    root = Path(root).resolve()
    path = root / relative_path
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValueError('RUNTIME_INPUT_MISSING_OR_OUTSIDE: ' + relative_path)
    return path


def asset_paths(root, spec=None):
    root = Path(root)
    spec = spec or load(root)
    result = {root / MANIFEST}
    for name in spec['assetFiles'] + [row['asset'] for row in spec['installs']]:
        result.add(_file(root, ASSETS + '/' + name))
    for name in spec['assetTrees']:
        directory = root / ASSETS / name
        if not directory.is_dir() or not directory.resolve().is_relative_to((root / ASSETS).resolve()):
            raise ValueError('RUNTIME_INPUT_TREE: ' + name)
        for path in directory.rglob('*'):
            if path.is_file():
                result.add(_file(root, path.relative_to(root).as_posix()))
    return sorted(result)


def launcher_paths(root, spec=None):
    root = Path(root)
    spec = spec or load(root)
    result = {}
    for name in spec['launcherSources']:
        result[name] = _file(root, JAVA + '/' + name)
    for name in spec['launcherTrees']:
        directory = root / name
        if not directory.is_dir():
            raise ValueError('RUNTIME_LAUNCHER_TREE: ' + name)
        suffix = '.java' if name.startswith(JAVA + '/') else '.so'
        for path in directory.rglob('*' + suffix):
            key = path.relative_to(root / JAVA).as_posix() if suffix == '.java' else path.relative_to(root).as_posix()
            result[key] = _file(root, path.relative_to(root).as_posix())
    for name in spec.get('generatorSources', []):
        result['generator:'+name]=_file(root,name)
    return dict(sorted(result.items()))
