<?php
/*
 * AvidBackup database helper (version __AVB_VERSION__), created __AVB_CREATED__.
 *
 * What this file does: when AvidBackup sends the secret token, it streams a read-only dump of
 * ONE database as a gzip-compressed SQL file. That is all. It never runs shell commands, never
 * writes to your database and never lists files. The token itself is not stored here, only
 * its SHA-256 hash.
 *
 * It is safe to delete this file at any time (AvidBackup: avidbackup helper remove).
 * Keep it private: the file contains your database login, like wp-config.php does.
 */

define('AVB_VERSION', '__AVB_VERSION__');
define('AVB_TOKEN_SHA256', '__AVB_TOKEN_SHA256__');
define('AVB_DB_HOST', '__AVB_DB_HOST__');
define('AVB_DB_PORT', __AVB_DB_PORT__);
define('AVB_DB_USER', '__AVB_DB_USER__');
define('AVB_DB_PASS', '__AVB_DB_PASS__');
define('AVB_DB_NAME', '__AVB_DB_NAME__');
define('AVB_ALLOWED_IPS', '__AVB_ALLOWED_IPS__'); // comma separated; empty = any address
define('AVB_MAX_FAILURES', 5);      // wrong tokens allowed ...
define('AVB_LOCK_SECONDS', 600);    // ... within this many seconds before locking out

@ini_set('display_errors', '0');
@ini_set('zlib.output_compression', '0');
@set_time_limit(0);
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');
header('X-Robots-Tag: noindex, nofollow');

function avb_json($code, $payload)
{
    http_response_code($code);
    header('Content-Type: application/json');
    echo json_encode($payload);
    exit;
}

function avb_fail($code, $message)
{
    avb_json($code, array('ok' => false, 'error' => $message));
}

function avb_state_file()
{
    return rtrim(sys_get_temp_dir(), '/\\') . DIRECTORY_SEPARATOR . 'avb-' . substr(hash('sha256', __FILE__), 0, 20) . '.json';
}

function avb_recent_failures()
{
    $file = avb_state_file();
    $times = array();
    if (is_file($file)) {
        $raw = @json_decode((string) @file_get_contents($file), true);
        if (is_array($raw)) {
            foreach ($raw as $t) {
                if (is_int($t) && $t > time() - AVB_LOCK_SECONDS) {
                    $times[] = $t;
                }
            }
        }
    }
    return $times;
}

function avb_record_failure()
{
    $times = avb_recent_failures();
    $times[] = time();
    @file_put_contents(avb_state_file(), json_encode($times), LOCK_EX);
}

function avb_clear_failures()
{
    @unlink(avb_state_file());
}

// ---- 1. method, address, lock-out, token ------------------------------------------------

if (!isset($_SERVER['REQUEST_METHOD']) || $_SERVER['REQUEST_METHOD'] !== 'POST') {
    avb_fail(405, 'POST only');
}

if (AVB_ALLOWED_IPS !== '') {
    $allowed = array_map('trim', explode(',', AVB_ALLOWED_IPS));
    $ip = isset($_SERVER['REMOTE_ADDR']) ? $_SERVER['REMOTE_ADDR'] : '';
    if (!in_array($ip, $allowed, true)) {
        avb_fail(403, 'Not allowed from this address');
    }
}

if (count(avb_recent_failures()) >= AVB_MAX_FAILURES) {
    avb_fail(429, 'Too many failed attempts. Try again later.');
}

$token = isset($_POST['token']) ? (string) $_POST['token'] : '';
if ($token === '' || !hash_equals(AVB_TOKEN_SHA256, hash('sha256', $token))) {
    avb_record_failure();
    usleep(400000);
    avb_fail(403, 'Token rejected');
}
avb_clear_failures();

$action = isset($_POST['action']) ? (string) $_POST['action'] : 'ping';

// ---- 2. actions that need no database --------------------------------------------------

if ($action === 'ping') {
    avb_json(200, array(
        'ok' => true,
        'helper' => 'avidbackup',
        'version' => AVB_VERSION,
        'php' => PHP_VERSION,
        'mysqli' => extension_loaded('mysqli'),
        'gzip' => function_exists('deflate_init'),
    ));
}

// ---- 3. database helpers -----------------------------------------------------------------

function avb_connect()
{
    if (!extension_loaded('mysqli')) {
        avb_fail(501, 'The PHP mysqli extension is not available on this server');
    }
    mysqli_report(MYSQLI_REPORT_OFF);
    $db = @new mysqli(AVB_DB_HOST, AVB_DB_USER, AVB_DB_PASS, AVB_DB_NAME, (int) AVB_DB_PORT);
    if ($db->connect_errno) {
        avb_fail(502, 'Could not connect to the database: ' . $db->connect_error);
    }
    $db->set_charset('utf8mb4');
    return $db;
}

function avb_ident($name)
{
    return '`' . str_replace('`', '``', $name) . '`';
}

if ($action === 'check') {
    $db = avb_connect();
    $res = $db->query('SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()');
    $count = $res ? (int) $res->fetch_row()[0] : 0;
    avb_json(200, array(
        'ok' => true,
        'database' => AVB_DB_NAME,
        'server' => $db->server_info,
        'tables' => $count,
    ));
}

if ($action === 'tables') {
    $db = avb_connect();
    $res = $db->query(
        'SELECT TABLE_NAME, ENGINE, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH FROM information_schema.TABLES '
        . "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE' ORDER BY (DATA_LENGTH + INDEX_LENGTH) DESC"
    );
    $out = array();
    while ($res && ($row = $res->fetch_row())) {
        $out[] = array(
            'table' => $row[0],
            'engine' => (string) $row[1],
            'rows' => (int) $row[2],
            'data_bytes' => (int) $row[3],
            'index_bytes' => (int) $row[4],
        );
    }
    avb_json(200, array('ok' => true, 'tables' => $out));
}

if ($action !== 'dump') {
    avb_fail(400, 'Unknown action');
}

// ---- 4. the dump ---------------------------------------------------------------------------

class AvbOut
{
    private $ctx = null;
    private $pending = 0;

    public function __construct($gzip)
    {
        if ($gzip) {
            $this->ctx = deflate_init(ZLIB_ENCODING_GZIP, array('level' => 6));
        }
    }

    public function write($data)
    {
        if ($this->ctx !== null) {
            $data = deflate_add($this->ctx, $data, ZLIB_NO_FLUSH);
        }
        if ($data !== '') {
            echo $data;
            $this->pending += strlen($data);
            if ($this->pending > 65536) {
                $this->flush();
            }
        }
    }

    public function flush()
    {
        $this->pending = 0;
        if (function_exists('ob_get_level')) {
            while (ob_get_level() > 0) {
                @ob_end_flush();
            }
        }
        flush();
        if (connection_aborted()) {
            exit;
        }
    }

    public function finish()
    {
        if ($this->ctx !== null) {
            echo deflate_add($this->ctx, '', ZLIB_FINISH);
        }
        $this->flush();
    }
}

function avb_value($db, $field, $value)
{
    if ($value === null) {
        return 'NULL';
    }
    $numeric = array(1, 2, 3, 4, 5, 8, 9, 13, 246);
    if (in_array($field->type, $numeric, true)) {
        return $value;
    }
    // Binary data (BLOB, BINARY, VARBINARY, GEOMETRY): write as a hex literal.
    $stringy = array(15, 249, 250, 251, 252, 253, 254, 255);
    if ($field->charsetnr === 63 && in_array($field->type, $stringy, true)) {
        return $value === '' ? "''" : '0x' . bin2hex($value);
    }
    return "'" . $db->real_escape_string($value) . "'";
}

function avb_dump_table($db, $out, $table)
{
    $q = avb_ident($table);
    $out->write("--\n-- Table structure for table $q\n--\n\nDROP TABLE IF EXISTS $q;\n");
    $res = $db->query("SHOW CREATE TABLE $q");
    $row = $res ? $res->fetch_row() : null;
    if (!$row) {
        $out->write("-- Could not read the structure of $q\n\n");
        return;
    }
    $out->write($row[1] . ";\n\n");
    $res->free();

    $out->write("--\n-- Data for table $q\n--\n\n");

    // BIT columns are fetched as explicit binary literals (b'101'). PHP's drivers return BIT as
    // either raw bytes or decimal text depending on the build, so never rely on that.
    $probe = $db->query("SELECT * FROM $q LIMIT 0");
    $select = array();
    $literal = array();
    if ($probe) {
        foreach ($probe->fetch_fields() as $i => $f) {
            $col = avb_ident($f->name);
            if ($f->type === 16) {
                $select[] = "CONCAT('b''', BIN($col + 0), '''') AS $col";
                $literal[$i] = true;
            } else {
                $select[] = $col;
            }
        }
        $probe->free();
    }
    if (!$select) {
        $out->write("-- Could not read the data of $q\n\n");
        return;
    }
    $res = $db->query('SELECT ' . implode(', ', $select) . " FROM $q", MYSQLI_USE_RESULT);
    if (!$res) {
        $out->write("-- Could not read the data of $q\n\n");
        return;
    }
    $fields = $res->fetch_fields();
    $batch = array();
    $size = 0;
    while ($row = $res->fetch_row()) {
        $parts = array();
        foreach ($row as $i => $value) {
            if (isset($literal[$i])) {
                $parts[] = $value === null ? 'NULL' : $value;
            } else {
                $parts[] = avb_value($db, $fields[$i], $value);
            }
        }
        $tuple = '(' . implode(',', $parts) . ')';
        $batch[] = $tuple;
        $size += strlen($tuple);
        if (count($batch) >= 200 || $size > 1048576) {
            $out->write("INSERT INTO $q VALUES " . implode(",\n", $batch) . ";\n");
            $batch = array();
            $size = 0;
        }
    }
    if ($batch) {
        $out->write("INSERT INTO $q VALUES " . implode(",\n", $batch) . ";\n");
    }
    $res->free();
    $out->write("\n");
}

function avb_dump_extras($db, $out, $views)
{
    foreach ($views as $view) {
        $q = avb_ident($view);
        $res = $db->query("SHOW CREATE VIEW $q");
        $row = $res ? $res->fetch_row() : null;
        if ($row) {
            $out->write("--\n-- View $q\n--\n\nDROP VIEW IF EXISTS $q;\n" . $row[1] . ";\n\n");
        }
    }
    $res = $db->query('SHOW TRIGGERS');
    $names = array();
    while ($res && ($row = $res->fetch_assoc())) {
        $names[] = $row['Trigger'];
    }
    foreach ($names as $name) {
        $q = avb_ident($name);
        $res = $db->query("SHOW CREATE TRIGGER $q");
        $row = $res ? $res->fetch_assoc() : null;
        if ($row && isset($row['SQL Original Statement'])) {
            $out->write("--\n-- Trigger $q\n--\n\nDROP TRIGGER IF EXISTS $q;\nDELIMITER ;;\n"
                . $row['SQL Original Statement'] . " ;;\nDELIMITER ;\n\n");
        }
    }
    foreach (array('PROCEDURE', 'FUNCTION') as $kind) {
        $res = $db->query("SHOW $kind STATUS WHERE Db = DATABASE()");
        $names = array();
        while ($res && ($row = $res->fetch_assoc())) {
            $names[] = $row['Name'];
        }
        foreach ($names as $name) {
            $q = avb_ident($name);
            $res = $db->query("SHOW CREATE $kind $q");
            $row = $res ? $res->fetch_row() : null;
            if ($row && isset($row[2])) {
                $out->write("--\n-- $kind $q\n--\n\nDROP $kind IF EXISTS $q;\nDELIMITER ;;\n"
                    . $row[2] . " ;;\nDELIMITER ;\n\n");
            }
        }
    }
}

$db = avb_connect();
$gzip = function_exists('deflate_init');

http_response_code(200);
header('Content-Type: application/octet-stream');
header('X-AvidBackup-Format: ' . ($gzip ? 'gzip' : 'plain'));
header('X-Accel-Buffering: no');
if (function_exists('ob_get_level')) {
    while (ob_get_level() > 0) {
        @ob_end_clean();
    }
}

$out = new AvbOut($gzip);
$out->write("-- AvidBackup PHP helper dump\n-- Database: " . AVB_DB_NAME . "\n-- Server version: " . $db->server_info
    . "\n-- Generated: " . gmdate('Y-m-d H:i:s') . " UTC\n\n");
$out->write("/*!40101 SET NAMES utf8mb4 */;\n/*!40014 SET FOREIGN_KEY_CHECKS=0 */;\n"
    . "/*!40101 SET SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;\nSET time_zone = '+00:00';\n\n");

// One consistent, read-only snapshot for the whole dump.
@$db->query('SET SESSION TRANSACTION READ ONLY');
@$db->query('START TRANSACTION WITH CONSISTENT SNAPSHOT');

$tables = array();
$views = array();
$res = $db->query('SHOW FULL TABLES');
while ($res && ($row = $res->fetch_row())) {
    if (strtoupper($row[1]) === 'VIEW') {
        $views[] = $row[0];
    } else {
        $tables[] = $row[0];
    }
}
if ($res) {
    $res->free();
}

foreach ($tables as $table) {
    avb_dump_table($db, $out, $table);
}
avb_dump_extras($db, $out, $views);

$out->write("/*!40014 SET FOREIGN_KEY_CHECKS=1 */;\n-- Dump completed on " . gmdate('Y-m-d H:i:s') . " UTC\n");
$out->finish();
$db->close();
exit;
