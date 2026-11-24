"""可选的第二个固定输入：hermes 运行时锁的读取与描述符证据。

为什么不放进 `runtime_input_contract.py`：那个文件属于 **dsh 运行时打包配方**
（`tools/build-dsh-runtime.py` 的 `recipe_inputs()` 会哈希它）。改动它会让
`prepare-standard-assets.py` 认定「已发布的 dsh-runtime.bin 与当前锁不一致」而拒绝构建。
hermes 是独立的第二个输入，锁与校验逻辑必须自成一体。

锁缺失时全部返回 None，dsh 的现有路径不受影响。
"""
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

ASSETS = 'app/src/main/assets'
HERMES_LOCK = 'ci/hermes-runtime.lock.json'
HERMES_ASSET = 'hermes-runtime.bin'
HERMES_META_ASSET = 'hermes-runtime.inputs.json'
HERMES_LOCK_FIELDS = ('asset', 'url', 'sha256', 'bytes', 'metaAsset', 'metaUrl', 'metaSha256',
                      'metaBytes', 'version', 'commit')


def _https(value):
    if not isinstance(value, str) or any(ord(c) < 32 or ord(c) == 127 or c.isspace() or c == '\\'
                                         for c in value):
        raise ValueError('HERMES_LOCK_URL')
    parsed = urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username is not None \
            or parsed.password is not None:
        raise ValueError('HERMES_LOCK_URL')


def _digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def hermes_lock(root):
    """读取可选 hermes 固定输入锁；文件不存在时返回 None（不报错、不影响 dsh）。"""
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
        if not isinstance(lock.get(key), str) \
                or not re.fullmatch(r'[A-Za-z0-9._+-]{1,80}', lock[key]):
            raise ValueError('HERMES_LOCK_IDENTITY: ' + key)
    return {key: lock[key] for key in HERMES_LOCK_FIELDS}


def hermes_section(root):
    """描述符的 hermes 段：锁存在时给出，本地归档存在时先核对字节。

    这不是 dsh 的身份字段（Java 侧 RuntimeDescriptor 只认固定字段表），因此过渡期
    不改 dshVersion / runtimeId；归档还没还原时只记录锁里的摘要，还原与字节校验由
    `tools/ci-assets.py --hermes` 负责。
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
