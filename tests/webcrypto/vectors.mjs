// Generate reference vectors with the SAME WebCrypto calls as the official
// web client (src/crypto.js + src/account.js + src/node.js).
const subtle = globalThis.crypto.subtle
const str2buf = s => Uint8Array.from(s, c => c.codePointAt(0))
const buf2str = b => String.fromCharCode(...new Uint8Array(b))
const b64 = s => btoa(s)
const urlb64 = s => btoa(s).replace(/[+\/=]/g, c => ({'+': '-', '/': '_', '=': ''})[c])
const sha256 = async s => buf2str(await subtle.digest({name: 'SHA-256'}, str2buf(s)))

const email = 'Bob.Example@Example.COM'
const passphrase = process.argv[2] || 'correct horse battery staple é'

// account.js create_secret()
const K = await subtle.generateKey({name: 'RSA-OAEP', modulusLength: 2048,
  publicExponent: new Uint8Array([1, 0, 1]), hash: {name: 'SHA-256'}}, true, ['encrypt', 'decrypt'])
const P = buf2str(await subtle.exportKey('spki', K.publicKey))
const salt = await sha256(email.toLowerCase())
const material = await subtle.importKey('raw', str2buf(passphrase), {name: 'PBKDF2'}, false, ['deriveKey'])
const L = await subtle.deriveKey({name: 'PBKDF2', hash: 'SHA-256', salt: str2buf(salt), iterations: 100000},
  material, {name: 'AES-CBC', length: 256}, true, ['encrypt', 'decrypt', 'wrapKey', 'unwrapKey'])
const H = await sha256(buf2str(await subtle.exportKey('raw', L)))
const W = buf2str(await subtle.wrapKey('pkcs8', K.privateKey, L, {name: 'AES-CBC', iv: str2buf(salt).slice(0, 16)}))

// node.js _login(): sign payload with the account key
const pkcs8 = buf2str(await subtle.exportKey('pkcs8', K.privateKey))
const sigkey = await subtle.importKey('pkcs8', str2buf(pkcs8), {name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256'}, true, ['sign'])
const payload = {time: '2026-10-25T12:00:00.000Z', session: 'abcdefghijklmnop'}
const signature = urlb64(buf2str(await subtle.sign('RSASSA-PKCS1-v1_5', sigkey, str2buf(JSON.stringify(payload)))))

// crypto.pubkey_id()
const jwk = await subtle.exportKey('jwk', K.publicKey)
const id = urlb64(await sha256(atob(jwk.n.replace(/-/g, '+').replace(/_/g, '/'))))

// A machine session key encrypted to the account key + an AES message
const pub = await subtle.importKey('spki', str2buf(P), {name: 'RSA-OAEP', hash: 'SHA-256'}, true, ['encrypt'])
const E = globalThis.crypto.getRandomValues(new Uint8Array(32))
const M = buf2str(await subtle.encrypt({name: 'RSA-OAEP'}, pub, E))
const aes = await subtle.importKey('raw', E, {name: 'AES-CBC'}, true, ['encrypt', 'decrypt'])
const iv = buf2str(globalThis.crypto.getRandomValues(new Uint8Array(16)))
const msg = JSON.stringify({type: 'message', session: 'abc', content: {cmd: 'state', state: 'fold'}})
const enc = buf2str(await subtle.encrypt({name: 'AES-CBC', iv: str2buf(iv)}, aes, str2buf(msg)))

console.log(JSON.stringify({
  email, passphrase, password_hash: b64(H), secret: b64(W), pubkey: b64(P), id,
  payload, signature, machine_key_encrypted: b64(M), machine_key: b64(buf2str(E)),
  iv: urlb64(iv), message_payload: urlb64(enc), message: msg,
}))
