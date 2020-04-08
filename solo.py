#!/usr/bin/env python3
from collections import namedtuple
import hashlib
import secrets
import struct
import sys
import time

from fido2.client import Fido2Client
from fido2.ctap2 import CTAP2
from fido2.hid import CtapHidDevice
from fido2.utils import hmac_sha256
from fido2.attestation import Attestation
from fido2.webauthn import PublicKeyCredentialCreationOptions

SubPacket = namedtuple('SubPacket', ['type', 'body'])

def verify_rp_id(rp_id, origin):
    return origin == rp_id

class SoloPGP(object):
    def __init__(self, dev):
        origin = "pgp"
        self.client = Fido2Client(dev, origin, verify=verify_rp_id)
        self.ctap2 = CTAP2(dev)
        # FIXME
        self.pin = '1111'

    def sign_hash(self, cred_id, dgst):
        if self.pin:
            pin_token = self.client.pin_protocol.get_pin_token(self.pin)
            pin_auth = hmac_sha256(pin_token, dgst)[:16]
            ret = self.ctap2.send_cbor(0x50, {1: dgst, 2: {"id": cred_id, "type": "public-key"}, 3: pin_auth})
        else:
            ret = self.ctap2.send_cbor(0x50, {1: dgst, 2: {"id": cred_id, "type": "public-key"}})
        der_sig = ret[1]
        # https://crypto.stackexchange.com/questions/1795/how-can-i-convert-a-der-ecdsa-signature-to-asn-1
        r_len = der_sig[3]
        r = der_sig[4:4+r_len]
        s = der_sig[6+r_len:]
        if len(r) > 32:
            r = r[-32:]
        if len(r) < 32:
            r = (32-len(r)) * b'\x00' + r
        if len(s) > 32:
            s = s[-32:]
        if len(s) < 32:
            s = (32-len(s)) * b'\x00' + s
        return r,s

    def pubkey_packet(self, pubkey, created):
        pkt = bytearray()
        pkt.append(0x98) # public key packet
        pkt.append(0x52) # packet length
        pkt.append(0x04) # version
        pkt.extend(struct.pack('>I', created))
        pkt.append(0x13) # ECDSA algo
        pkt.extend(b'\x08\x2A\x86\x48\xCE\x3D\x03\x01\x07') # nistp256 id
        pkt.extend(b'\x02\x03') # 0x203 bits MPI
        pkt.append(0x04) # uncompressed key
        pkt.extend(pubkey[0]) # pubkey x
        pkt.extend(pubkey[1]) # pubkey y
        return pkt

    def fingerprint(self, pubkey_pkt):
        fp = b'\x99\x00\x52' + pubkey_pkt[2:]
        m = hashlib.sha1()
        m.update(fp)
        return m.digest()

    def userid_packet(self, user):
        pkt = bytearray()
        pkt.append(0xb4)
        pkt.append(len(user))
        pkt.extend(user.encode('ascii'))
        return pkt

    def signature_packet(self, sig_type, cred_id, hashed_prefix, hashed_subpkts, unhashed_subpkts):
        pkt = bytearray()
        pkt.append(0x04) # version
        pkt.append(sig_type)
        pkt.append(0x13) # ECDSA algo
        pkt.append(0x08) # SHA256
        hashed_count = sum([1+len(subpkt.body) for subpkt in hashed_subpkts])
        hashed_count += len(hashed_subpkts)
        pkt.extend(struct.pack('>H', hashed_count))
        for subpkt in hashed_subpkts:
            subpkt_len = len(subpkt.body) + 1
            pkt.extend(struct.pack('B', subpkt_len))
            pkt.append(subpkt.type)
            pkt.extend(subpkt.body)

        hashed_data = bytearray()
        hashed_data.extend(hashed_prefix)
        hashed_data.extend(pkt)
        hashed_data.extend(b'\x04\xff') # some PGP fuckery
        hashed_data.extend(struct.pack('>I', len(pkt)))

        unhashed_count = sum([1+len(subpkt.body) for subpkt in unhashed_subpkts])
        unhashed_count += len(unhashed_subpkts)
        pkt.extend(struct.pack('>H', unhashed_count))
        for subpkt in unhashed_subpkts:
            subpkt_len = len(subpkt.body) + 1
            pkt.extend(struct.pack('B', subpkt_len))
            pkt.append(subpkt.type)
            pkt.extend(subpkt.body)

        m = hashlib.sha256()
        m.update(hashed_data)
        dgst = m.digest()

        pkt.extend(dgst[:2]) # left 16 bits of the hash
        r, s = self.sign_hash(cred_id, dgst)
        ri = int.from_bytes(r, 'big', signed=False)
        si = int.from_bytes(s, 'big', signed=False)

        pkt.extend(struct.pack('>H', ri.bit_length()))
        pkt.extend(r)
        pkt.extend(struct.pack('>H', si.bit_length()))
        pkt.extend(s)
        pkt_len = len(pkt)
        pkt.insert(0, 0x88) # signature packet
        pkt.insert(1, pkt_len)
        return pkt

    def signature_packet_key(self, cred_id, hashed_prefix, hashed_subpkts, unhashed_subpkts):
        return self.signature_packet(0x13, cred_id, hashed_prefix, hashed_subpkts, unhashed_subpkts)

    def signature_packet_data(self, cred_id, hashed_prefix, hashed_subpkts, unhashed_subpkts):
        return self.signature_packet(0x00, cred_id, hashed_prefix, hashed_subpkts, unhashed_subpkts)

    def gen_key(self):
        name = input('Real name: ')
        email = input('Email address: ')
        username = "{} <{}>".format(name, email)
        created = int(time.time())
        rp = {"id": "pgp", "name": "OpenPGP"}
        user = {"id": struct.pack('>I', created), "name": username}
        challenge = secrets.token_bytes(32)
        options = PublicKeyCredentialCreationOptions(
            rp,
            user,
            challenge,
            [{"type": "public-key", "alg": -8},
             {"type": "public-key", "alg": -7}],
            authenticator_selection={"require_resident_key": True}
        )

        attestation_object, client_data = self.client.make_credential(options, pin=self.pin)
        statement = attestation_object.att_statement
        auth_data = attestation_object.auth_data
        attestation = Attestation.for_type("packed")()
        attestation.verify(statement, auth_data, client_data.hash)

        cred_id = auth_data.credential_data.credential_id
        pubkey_x = auth_data.credential_data.public_key[-2]
        pubkey_y = auth_data.credential_data.public_key[-3]

        pubkey = (pubkey_x, pubkey_y)
        pubkey_pkt = self.pubkey_packet(pubkey, created)
        userid_pkt = self.userid_packet(username)
        fp = self.fingerprint(pubkey_pkt)
        key_id = fp[-8:]
        print("Key ID: {}".format(key_id.hex()))
        print("Key fingerprint: {}".format(fp.hex()))

        hashed_prefix = b'\x99\x00\x52' + pubkey_pkt[2:]
        hashed_prefix += b'\xb4' + struct.pack('>I', len(userid_pkt)-2) + userid_pkt[2:]
        hashed_subpkts = [SubPacket(0x21, b'\x04'+fp),
                          SubPacket(0x02, struct.pack('>I', created))]
        unhashed_subpkts = [SubPacket(0x10, key_id)] # issuer
        sig_pkt = self.signature_packet_key(cred_id, hashed_prefix, hashed_subpkts, unhashed_subpkts)

        with open('pkt', 'wb') as f:
            f.write(pubkey_pkt)
            f.write(userid_pkt)
            f.write(sig_pkt)

if __name__=='__main__':
    hid_devices = list(CtapHidDevice.list_devices())
    dev = hid_devices[0]
    solo = SoloPGP(dev)
    solo.gen_key()

