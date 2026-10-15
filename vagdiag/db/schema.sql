-- VAGDiag Studio - Datenbankschema
-- Alle Tabellen sind bewusst schlank gehalten und werden aus den Seed-Dateien
-- unter vagdiag/db/seed/ aufgebaut (siehe docs/DATENBANK.md).

PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------------------- --
-- Metadaten
-- ------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);

-- ------------------------------------------------------------------------- --
-- Steuergeraeteadressen (VAG-Adressen, 0x01 .. 0xFF)
-- ------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS control_module (
    address     INTEGER PRIMARY KEY,          -- Dezimalwert der VAG-Adresse (1 = 0x01)
    name        TEXT NOT NULL,                -- deutsche Bezeichnung
    name_en     TEXT,
    category    TEXT,                          -- Motor, Antrieb, Komfort, ...
    description TEXT,
    obd_relevant INTEGER NOT NULL DEFAULT 0,   -- erscheint im OBD-II/EOBD-Bereich
    source      TEXT
);

-- ------------------------------------------------------------------------- --
-- Fahrzeugplattformen, Fahrzeuge und deren Steuergeraeteausstattung
-- ------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS platform (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    years       TEXT,
    protocol    TEXT,                          -- KWP1281, KWP2000, UDS, ...
    bus         TEXT,                          -- K-Line, CAN, CAN+KL
    notes       TEXT,
    source      TEXT
);

CREATE TABLE IF NOT EXISTS vehicle (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    platform_id INTEGER REFERENCES platform(id) ON DELETE CASCADE,
    model_year  INTEGER,
    engine      TEXT,
    market      TEXT,
    notes       TEXT,
    source      TEXT
);

CREATE TABLE IF NOT EXISTS vehicle_module (
    vehicle_id     INTEGER NOT NULL REFERENCES vehicle(id) ON DELETE CASCADE,
    address        INTEGER NOT NULL REFERENCES control_module(address),
    protocol       TEXT,                       -- KWP1281, KWP2000, UDS, OBD
    bus            TEXT,                       -- K-Line, CAN
    part_number    TEXT,
    component      TEXT,
    software       TEXT,
    coding         TEXT,
    wsc            TEXT,
    label_file     TEXT,                       -- Verweis auf label_file.name
    dtc_set        TEXT,                       -- Kennung des Fehlersatzes (Simulation)
    primary key (vehicle_id, address)
);

-- ------------------------------------------------------------------------- --
-- Label-Dateien, Messwertbloecke und Codierhilfen
-- ------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS label_file (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,          -- Dateiname der .lbl-Datei
    part_number TEXT,                          -- Teilenummer, zu der sie gehoert
    vehicle_id  INTEGER REFERENCES vehicle(id) ON DELETE SET NULL,
    notes       TEXT,
    source      TEXT
);

CREATE TABLE IF NOT EXISTS label_redirect (
    label_id    INTEGER NOT NULL REFERENCES label_file(id) ON DELETE CASCADE,
    target      TEXT NOT NULL,
    PRIMARY KEY (label_id, target)
);

CREATE TABLE IF NOT EXISTS measuring_block (
    id          INTEGER PRIMARY KEY,
    label_id    INTEGER NOT NULL REFERENCES label_file(id) ON DELETE CASCADE,
    block       INTEGER NOT NULL,
    field       INTEGER NOT NULL,               -- 0 = Blocktitel
    basic       INTEGER NOT NULL DEFAULT 0,     -- 1 = Eintrag der Grundeinstellung (B...)
    text        TEXT,
    short_text  TEXT,
    unit        TEXT,
    min_value   REAL,
    max_value   REAL,
    UNIQUE (label_id, block, field, basic)
);

CREATE TABLE IF NOT EXISTS coding_entry (
    id          INTEGER PRIMARY KEY,
    label_id    INTEGER NOT NULL REFERENCES label_file(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,                  -- "C" = Codierung, "L" = Login/Anpassung
    block       INTEGER NOT NULL,
    bit         INTEGER,                        -- Bitposition in der Codierung (optional)
    value       INTEGER,
    text        TEXT
);

-- ------------------------------------------------------------------------- --
-- Fehlercodes
-- ------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS dtc (
    code        TEXT PRIMARY KEY,               -- "00575" oder "P0420"
    kind        TEXT NOT NULL,                  -- "vag" | "sae"
    vag_code    INTEGER,
    sae_code    TEXT,
    text        TEXT NOT NULL,                  -- deutsche Beschreibung
    text_short  TEXT,
    system      TEXT,                           -- Motor, ABS, Airbag, ...
    status_hint TEXT,                           -- Hinweis zum Statusbyte
    source      TEXT
);

CREATE INDEX IF NOT EXISTS idx_dtc_vag ON dtc(vag_code);
CREATE INDEX IF NOT EXISTS idx_dtc_sae ON dtc(sae_code);

-- ------------------------------------------------------------------------- --
-- Anpassungskanaele (Beschreibung pro Steuergeraet/Label)
-- ------------------------------------------------------------------------- --
CREATE TABLE IF NOT EXISTS adaptation_channel (
    id          INTEGER PRIMARY KEY,
    label_id    INTEGER REFERENCES label_file(id) ON DELETE CASCADE,
    module_address INTEGER REFERENCES control_module(address),
    channel     INTEGER NOT NULL,
    text        TEXT,
    min_value   INTEGER,
    max_value   INTEGER,
    unit        TEXT,
    source      TEXT
);

-- ------------------------------------------------------------------------- --
-- Sicht: Steuergeraete je Fahrzeug inklusive Klartext
-- ------------------------------------------------------------------------- --
CREATE VIEW IF NOT EXISTS vehicle_module_view AS
SELECT
    v.name              AS vehicle,
    vm.address          AS address,
    printf('%02d', vm.address) AS address_text,
    COALESCE(cm.name, 'Unbekannt') AS module,
    cm.category         AS category,
    vm.protocol         AS protocol,
    vm.bus              AS bus,
    vm.part_number      AS part_number,
    vm.component        AS component,
    vm.coding           AS coding,
    vm.label_file       AS label_file
FROM vehicle_module vm
JOIN vehicle v ON v.id = vm.vehicle_id
LEFT JOIN control_module cm ON cm.address = vm.address
ORDER BY vm.address;
