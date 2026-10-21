# Sync protocol: aeradex-team (the book) ↔ aeradex-team-portal

The portal is a mailbox and a read-only mirror. The book lives only where aeradex runs (a PC or a server); the
book always initiates. All sync calls: `Authorization: Bearer <sync token>` (the portal stores only its SHA-256),
JSON bodies, UTF-8.
Base path: `/app/api/sync`.

## Entries (employees → book)

Employees post entries with a client-generated UUID (idempotent; the phone may resend). Kinds:

| kind | data |
|---|---|
| `zeit` | `datum` (JJJJ-MM-TT), `stunden` ("3.5"), `kunde` (K0001; empty = intern), `projekt` (optional, of that customer), `leistung` (Leistungsart, optional), `kategorie` (intern only), `text` |
| `material` | `datum`, `kunde` (required), `projekt` (optional), `produkt`, `menge`, `text` |
| `abwesenheit` | `art` (Ferien, Kompensation, Unbezahlt, Weiterbildung → **Antrag**; Krank, Unfall → direct), `von`, `bis`, `halb` (bool), `notiz` |

Status on the portal: `offen` → (`in_uebernahme`, claimed by a pull; back to `offen` after 10 min without ack)
→ `uebernommen` | `abgelehnt` | `geloescht` (row removed).

**Changing booked entries.** An entry keeps its uuid for life. While the office's last snapshot marks it
`aenderbar` (time recording not closed up to that day, not invoiced, request still open), the employee may change
it (same uuid, new data, `version` = booked version + 1) or delete it (same, data carries `"_loeschen": true`).
The book replaces resp. removes the booked entry in one transaction and records the new version in
`team/importiert.yaml`. The book decides: a change that arrives after the office closed the period is acked
`abgelehnt` with the reason, the booked entry stays as it was.

### `GET /pending`
```json
{"entries": [{"uuid": "…", "person": "M0001", "kind": "zeit", "version": 2, "data": {…}, "created": "…"}],
 "keys": [{"person": "M0001", "pubkey": "<SPKI DER base64>", "key_version": 3}],
 "server_time": "2026-10-15T12:00:00+00:00"}
```
Returned `offen` entries become `in_uebernahme`.

### `POST /ack`
```json
{"results": [{"uuid": "…", "version": 2, "status": "uebernommen", "aeradex_id": "L-2026-0042", "reason": ""}]}
```
The book imports each UUID at most once: `team/importiert.yaml` (uuid → aeradex id) is written in the same
transaction as the entry. A replayed UUID is acked with its existing id.

## Snapshot (book → employees)

### `PUT /snapshot`
```json
{"stand": "2026-10-15T12:00:00+02:00",
 "shared": {"firma": "…", "kunden": [{"nummer": "K0001", "name": "…", "kontakt": "…", "strasse": "…", "ort": "3600 Thun", "land": ""}],
            "projekte": [{"nummer": "P0001", "name": "…", "kunde_nr": "K0001", "kunde": "…"}],
            "leistungen": [{"nummer": "P001", "text": "Arbeitszeit"}],
            "material": [{"nummer": "P010", "text": "Kleinmaterial", "einheit": "Stk"}],
            "kategorien": ["Intern", "Werkstatt"], "abwesenheiten": ["Ferien", "Krank", …],
            "feiertage": {"2026-12-25": "Weihnachtstag"}},
 "people": {"M0001": {"name": "…", "konto": {…}, "tage": {"2026-10-15": "8.40"}, "eintraege": [...],
                      "abwesenheiten": [...], "antraege": [...]}}}
```
Entries, absences and requests recorded in the app carry `uuid`, `version`, `kind`, `data` (form fields),
`aenderbar` and `gesperrt` (`abgeschlossen` | `verrechnet` | request status). Per person also `abgeschlossen_bis`.
Customers carry their address (employees drive to the site), never e-mail, notes or rates. No prices,
rates or salaries are ever sent in a snapshot. Salaries only travel inside
encrypted documents.

## Accounts (book → portal)

### `PUT /accounts`
```json
{"accounts": [{"person": "M0001", "benutzer": "anna", "name": "Anna Muster", "aktiv": true}],
 "invites": [{"person": "M0001", "code_sha256": "…", "expires": "2026-10-29"}]}
```
An invite resets the account: password, key pair and sessions are cleared; documents encrypted to the old key
are deleted and re-uploaded by the book once the new public key arrives. Accounts missing from the list are deactivated.

## Documents (book → employees, end-to-end encrypted)

### `GET /docs` → `{"docs": [{"person", "doc_id", "sha256", "key_version"}]}`
### `PUT /docs/{person}/{doc_id}`
```json
{"title": "Lohnabrechnung Oktober 2026", "kind": "lohn", "period": "2026-10", "sha256": "<of plaintext source>",
 "key_version": 3, "epk": "<ephemeral SPKI DER base64>", "iv": "<12 bytes base64>", "ct": "<base64>"}
```
### `DELETE /docs/{person}/{doc_id}`

**Kennung.** One word per installation (`AERADEX_PORTAL_KENNUNG`, default `aeradex`) names it in the protocol:
session cookie `<kennung>_s` (`-` → `_`), CSRF header `x-<kennung>: 1` on every write from a phone, the phone's
IndexedDB `<kennung>-team`, and the salts below. `GET /docs` answers `{"docs": […], "kennung": "…"}`, so the book
encrypts with the portal's Kennung. Changing it logs everyone out and makes stored documents unreadable.

Encryption (`<kennung>-doc-v1`): ECDH P-256 between an ephemeral key and the employee's public key →
HKDF-SHA256 (salt `<kennung>-doc-salt-v1`, info `<kennung>-doc-v1`, 32 bytes) → AES-256-GCM, AAD `"{person}/{doc_id}"`.

Employee key pair: generated in the browser at activation. The private key (PKCS#8) is wrapped with
AES-256-GCM under PBKDF2-SHA256(password, 16-byte salt, 600 000 iterations) and stored on the portal as
`{"salt", "iv", "ct", "iter"}`. The portal never sees the password: login sends
`auth = hex(PBKDF2-SHA256(password, "<kennung>-auth:" + benutzer, 100 000, 32 bytes))`, which the portal hashes with scrypt.
