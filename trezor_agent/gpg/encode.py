"""Create GPG ECDSA signatures and public keys using TREZOR device."""
import base64
import hashlib
import logging
import struct
import time

from . import agent, decode
from .. import client, factory, formats, util

log = logging.getLogger(__name__)


def packet(tag, blob):
    """Create small GPG packet."""
    assert len(blob) < 2**32

    if len(blob) < 2**8:
        length_type = 0
    elif len(blob) < 2**16:
        length_type = 1
    else:
        length_type = 2

    fmt = ['>B', '>H', '>L'][length_type]
    leading_byte = 0x80 | (tag << 2) | (length_type)
    return struct.pack('>B', leading_byte) + util.prefix_len(fmt, blob)


def subpacket(subpacket_type, fmt, *values):
    """Create GPG subpacket."""
    blob = struct.pack(fmt, *values) if values else fmt
    return struct.pack('>B', subpacket_type) + blob


def subpacket_long(subpacket_type, value):
    """Create GPG subpacket with 32-bit unsigned integer."""
    return subpacket(subpacket_type, '>L', value)


def subpacket_time(value):
    """Create GPG subpacket with time in seconds (since Epoch)."""
    return subpacket_long(2, value)


def subpacket_byte(subpacket_type, value):
    """Create GPG subpacket with 8-bit unsigned integer."""
    return subpacket(subpacket_type, '>B', value)


def subpackets(*items):
    """Serialize several GPG subpackets."""
    prefixed = [util.prefix_len('>B', item) for item in items]
    return util.prefix_len('>H', b''.join(prefixed))


def mpi(value):
    """Serialize multipresicion integer using GPG format."""
    bits = value.bit_length()
    data_size = (bits + 7) // 8
    data_bytes = bytearray(data_size)
    for i in range(data_size):
        data_bytes[i] = value & 0xFF
        value = value >> 8

    data_bytes.reverse()
    return struct.pack('>H', bits) + bytes(data_bytes)


def _serialize_nist256(vk):
    return mpi((4 << 512) |
               (vk.pubkey.point.x() << 256) |
               (vk.pubkey.point.y()))


def _serialize_ed25519(vk):
    return mpi((0x40 << 256) |
               util.bytes2num(vk.to_bytes()))


SUPPORTED_CURVES = {
    formats.CURVE_NIST256: {
        # https://tools.ietf.org/html/rfc6637#section-11
        'oid': b'\x2A\x86\x48\xCE\x3D\x03\x01\x07',
        'algo_id': 19,
        'serialize': _serialize_nist256
    },
    formats.CURVE_ED25519: {
        'oid': b'\x2B\x06\x01\x04\x01\xDA\x47\x0F\x01',
        'algo_id': 22,
        'serialize': _serialize_ed25519
    }
}


def _find_curve_by_algo_id(algo_id):
    curve_name, = [name for name, info in SUPPORTED_CURVES.items()
                   if info['algo_id'] == algo_id]
    return curve_name


class HardwareSigner(object):
    """Sign messages and get public keys from a hardware device."""

    def __init__(self, user_id, curve_name):
        """Connect to the device and retrieve required public key."""
        self.client_wrapper = factory.load()
        self.identity = self.client_wrapper.identity_type()
        self.identity.proto = 'gpg'
        self.identity.host = user_id
        self.curve_name = curve_name

    def pubkey(self):
        """Return public key as VerifyingKey object."""
        addr = client.get_address(self.identity)
        public_node = self.client_wrapper.connection.get_public_node(
            n=addr, ecdsa_curve_name=self.curve_name)

        return formats.decompress_pubkey(
            pubkey=public_node.node.public_key,
            curve_name=self.curve_name)

    def sign(self, digest):
        """Sign the digest and return a serialized signature."""
        result = self.client_wrapper.connection.sign_identity(
            identity=self.identity,
            challenge_hidden=digest,
            challenge_visual=util.hexlify(digest),
            ecdsa_curve_name=self.curve_name)
        assert result.signature[:1] == b'\x00'
        sig = result.signature[1:]
        return mpi(util.bytes2num(sig[:32])) + mpi(util.bytes2num(sig[32:]))

    def close(self):
        """Close the connection to the device."""
        self.client_wrapper.connection.clear_session()
        self.client_wrapper.connection.close()


class AgentSigner(object):
    """Sign messages and get public keys using gpg-agent tool."""

    def __init__(self, user_id):
        """Connect to the agent and retrieve required public key."""
        self.sock = agent.connect()
        self.keygrip = agent.get_keygrip(user_id)
        self.public_key = decode.load_from_gpg(user_id)

    def pubkey(self):
        """Return public key as VerifyingKey object."""
        return self.public_key['verifying_key']

    def sign(self, digest):
        """Sign the digest and return an ECDSA signature."""
        params = agent.sign(sock=self.sock,
                            keygrip=self.keygrip, digest=digest)
        return b''.join(mpi(p) for p in params)

    def close(self):
        """Close the connection to gpg-agent."""
        self.sock.close()


class PublicKey(object):
    """GPG representation for public key packets."""

    def __init__(self, curve_name, created, verifying_key):
        """Contruct using a ECDSA VerifyingKey object."""
        self.curve_info = SUPPORTED_CURVES[curve_name]
        self.created = int(created)  # time since Epoch
        self.verifying_key = verifying_key
        self.algo_id = self.curve_info['algo_id']

    def data(self):
        """Data for packet creation."""
        header = struct.pack('>BLB',
                             4,             # version
                             self.created,  # creation
                             self.algo_id)  # public key algorithm ID
        oid = util.prefix_len('>B', self.curve_info['oid'])
        blob = self.curve_info['serialize'](self.verifying_key)
        return header + oid + blob

    def data_to_hash(self):
        """Data for digest computation."""
        return b'\x99' + util.prefix_len('>H', self.data())

    def _fingerprint(self):
        return hashlib.sha1(self.data_to_hash()).digest()

    def key_id(self):
        """Short (8 byte) GPG key ID."""
        return self._fingerprint()[-8:]

    def __repr__(self):
        """Short (8 hexadecimal digits) GPG key ID."""
        return '<{}>'.format(util.hexlify(self.key_id()))

    __str__ = __repr__


class Signer(object):
    """Performs GPG signing operations."""

    def __init__(self, user_id, created, curve_name):
        """Construct and loads a public key from the device."""
        self.user_id = user_id
        assert curve_name in formats.SUPPORTED_CURVES

        self.conn = HardwareSigner(user_id, curve_name=curve_name)
        self.pubkey = PublicKey(
            curve_name=curve_name, created=created,
            verifying_key=self.conn.pubkey())

        log.info('%s GPG public key %s created at %s', curve_name,
                 self.pubkey, util.time_format(self.pubkey.created))

    @classmethod
    def from_public_key(cls, pubkey, user_id):
        """
        Create from an existing GPG public key.

        `pubkey` should be loaded via `decode.load_from_gpg(user_id)`
        from the local GPG keyring.
        """
        s = Signer(user_id=user_id,
                   created=pubkey['created'],
                   curve_name=_find_curve_by_algo_id(pubkey['algo']))
        assert s.pubkey.key_id() == pubkey['key_id']
        return s

    def close(self):
        """Close connection and turn off the screen of the device."""
        self.conn.close()

    def export(self):
        """Export GPG public key, ready for "gpg2 --import"."""
        pubkey_packet = packet(tag=6, blob=self.pubkey.data())
        user_id_packet = packet(tag=13, blob=self.user_id)

        data_to_sign = (self.pubkey.data_to_hash() +
                        user_id_packet[:1] +
                        util.prefix_len('>L', self.user_id))
        log.info('signing public key "%s"', self.user_id)
        hashed_subpackets = [
            subpacket_time(self.pubkey.created),  # signature creaion time
            subpacket_byte(0x1B, 1 | 2),  # key flags (certify & sign)
            subpacket_byte(0x15, 8),  # preferred hash (SHA256)
            subpacket_byte(0x16, 0),  # preferred compression (none)
            subpacket_byte(0x17, 0x80)]  # key server prefs (no-modify)
        unhashed_subpackets = [
            subpacket(16, self.pubkey.key_id())]  # issuer key id

        signature = _make_signature(
            signer_func=self.conn.sign,
            public_algo=self.pubkey.algo_id,
            data_to_sign=data_to_sign,
            sig_type=0x13,  # user id & public key
            hashed_subpackets=hashed_subpackets,
            unhashed_subpackets=unhashed_subpackets)

        sign_packet = packet(tag=2, blob=signature)
        return pubkey_packet + user_id_packet + sign_packet

    def subkey(self):
        """Export a subkey to `self.user_id` GPG primary key."""
        subkey_packet = packet(tag=14, blob=self.pubkey.data())
        primary = decode.load_from_gpg(self.user_id)
        keygrip = agent.get_keygrip(self.user_id)
        log.info('adding as subkey to %s (%s)', self.user_id, keygrip)
        data_to_sign = primary['_to_hash'] + self.pubkey.data_to_hash()

        # Primary Key Binding Signature
        hashed_subpackets = [
            subpacket_time(self.pubkey.created)]  # signature creaion time
        unhashed_subpackets = [
            subpacket(16, self.pubkey.key_id())]  # issuer key id
        embedded_sig = _make_signature(signer_func=self.conn.sign,
                                       data_to_sign=data_to_sign,
                                       public_algo=self.pubkey.algo_id,
                                       sig_type=0x19,
                                       hashed_subpackets=hashed_subpackets,
                                       unhashed_subpackets=unhashed_subpackets)
        log.info('embedded signature: %r', embedded_sig)

        # Subkey Binding Signature
        hashed_subpackets = [
            subpacket_time(self.pubkey.created),  # signature creaion time
            subpacket_byte(0x1B, 2)]  # key flags (certify & sign)
        unhashed_subpackets = [
            subpacket(16, primary['key_id']),  # issuer key id
            subpacket(32, embedded_sig)]
        gpg_agent = AgentSigner(self.user_id)
        signature = _make_signature(signer_func=gpg_agent.sign,
                                    data_to_sign=data_to_sign,
                                    public_algo=primary['algo'],
                                    sig_type=0x18,
                                    hashed_subpackets=hashed_subpackets,
                                    unhashed_subpackets=unhashed_subpackets)
        sign_packet = packet(tag=2, blob=signature)
        return subkey_packet + sign_packet

    def sign(self, msg, sign_time=None):
        """Sign GPG message at specified time."""
        if sign_time is None:
            sign_time = int(time.time())

        log.info('signing %d byte message at %s',
                 len(msg), util.time_format(sign_time))
        hashed_subpackets = [subpacket_time(sign_time)]
        unhashed_subpackets = [
            subpacket(16, self.pubkey.key_id())]  # issuer key id

        blob = _make_signature(signer_func=self.conn.sign,
                               data_to_sign=msg,
                               public_algo=self.pubkey.algo_id,
                               hashed_subpackets=hashed_subpackets,
                               unhashed_subpackets=unhashed_subpackets)
        return packet(tag=2, blob=blob)


def _make_signature(signer_func, data_to_sign, public_algo,
                    hashed_subpackets, unhashed_subpackets, sig_type=0):
    # pylint: disable=too-many-arguments
    header = struct.pack('>BBBB',
                         4,         # version
                         sig_type,  # rfc4880 (section-5.2.1)
                         public_algo,
                         8)         # hash_alg (SHA256)
    hashed = subpackets(*hashed_subpackets)
    unhashed = subpackets(*unhashed_subpackets)
    tail = b'\x04\xff' + struct.pack('>L', len(header) + len(hashed))
    data_to_hash = data_to_sign + header + hashed + tail

    log.debug('hashing %d bytes', len(data_to_hash))
    digest = hashlib.sha256(data_to_hash).digest()

    sig = signer_func(digest=digest)

    return bytes(header + hashed + unhashed +
                 digest[:2] +  # used for decoder's sanity check
                 sig)  # actual ECDSA signature


def _split_lines(body, size):
    lines = []
    for i in range(0, len(body), size):
        lines.append(body[i:i+size] + '\n')
    return ''.join(lines)


def armor(blob, type_str):
    """See https://tools.ietf.org/html/rfc4880#section-6 for details."""
    head = '-----BEGIN PGP {}-----\nVersion: GnuPG v2\n\n'.format(type_str)
    body = base64.b64encode(blob)
    checksum = base64.b64encode(util.crc24(blob))
    tail = '-----END PGP {}-----\n'.format(type_str)
    return head + _split_lines(body, 64) + '=' + checksum + '\n' + tail