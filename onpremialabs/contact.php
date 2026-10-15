<?php
/**
 * onpremIA Labs — contact form endpoint (Hostinger, PHP 8.1+ with mbstring).
 *
 * Receives the website's contact form as JSON and emails it to the lab's
 * mailbox with PHP's mail(), which Hostinger delivers through its own MTA.
 * Settings live in contact.config.php (see contact.config.example.php).
 *
 * Request:  POST application/json
 *           {name, email, organization?, topic, message, language?, _honey?}
 *           (the fields of the previous site, nombre/mensaje, are accepted too)
 * Response: {"success": true} or {"success": false, "error": "..."}
 *
 * Deliverability: the email is sent From the lab's own mailbox, with the
 * visitor in Reply-To. Sending it "From" the visitor's address, as the
 * previous script did, makes SPF and DMARC fail for every visitor whose
 * domain is not onpremialabs.com, and the message lands in spam or is
 * rejected. Pressing Reply still answers the visitor.
 */
declare(strict_types=1);

header('Content-Type: application/json; charset=utf-8');
header('X-Content-Type-Options: nosniff');
header('Cache-Control: no-store');

function respond(int $status, array $body): never
{
    http_response_code($status);
    echo json_encode($body, JSON_UNESCAPED_UNICODE);
    exit;
}

function fail(int $status, string $error): never
{
    respond($status, ['success' => false, 'error' => $error]);
}

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    header('Allow: POST');
    fail(405, 'Method not allowed.');
}

// ------------------------------------------------------------------ config

$configPath = __DIR__ . '/contact.config.php';
if (!is_readable($configPath)) {
    fail(500, 'contact.config.php is missing on the server.');
}
/** @var array<string, mixed> $config */
$config = require $configPath;

$toEmail = trim((string) ($config['to_email'] ?? ''));
$envelopeFrom = trim((string) ($config['envelope_from'] ?? $config['from_email'] ?? ''));
$subjectPrefix = trim((string) ($config['subject'] ?? '[onpremIA Labs] Contact'));
$rateLimitMax = max(1, (int) ($config['rate_limit_max'] ?? 5));
$rateLimitWindow = max(60, (int) ($config['rate_limit_window_sec'] ?? 600));
$allowedOrigins = array_map('strval', (array) ($config['allowed_origins'] ?? []));

if (!filter_var($toEmail, FILTER_VALIDATE_EMAIL) || !filter_var($envelopeFrom, FILTER_VALIDATE_EMAIL)) {
    fail(500, 'Invalid mail settings. Check contact.config.php.');
}

// A browser posting from another site sends its Origin; refuse it, so the
// endpoint cannot be used as a mail relay by someone else's page.
$origin = (string) ($_SERVER['HTTP_ORIGIN'] ?? '');
if ($origin !== '' && $allowedOrigins !== [] && !in_array($origin, $allowedOrigins, true)) {
    fail(403, 'Origin not allowed.');
}

// ------------------------------------------------------------------ input

$raw = file_get_contents('php://input', false, null, 0, 20000) ?: '';
$payload = json_decode($raw, true);
if (!is_array($payload)) {
    fail(400, 'Invalid JSON.');
}

/** One line of text: no control characters, so nothing can break a header. */
function line(mixed $value): string
{
    return trim((string) preg_replace('/[\x00-\x1F\x7F]+/u', ' ', (string) $value));
}

/** Free text: keeps line breaks, drops every other control character. */
function text(mixed $value): string
{
    $value = str_replace(["\r\n", "\r"], "\n", (string) $value);
    return trim((string) preg_replace('/[\x00-\x08\x0B-\x1F\x7F]+/u', '', $value));
}

$name = line($payload['name'] ?? $payload['nombre'] ?? '');
$email = line($payload['email'] ?? '');
$organization = line($payload['organization'] ?? '');
$topic = line($payload['topic'] ?? '');
$message = text($payload['message'] ?? $payload['mensaje'] ?? '');
$language = line($payload['language'] ?? '');
$honeypot = line($payload['_honey'] ?? '');

// A bot that filled the hidden field is told it worked and nothing is sent.
if ($honeypot !== '') {
    respond(200, ['success' => true]);
}

$topics = ['OPAAS', 'Evolve', 'NexoDual', 'Partnership', 'Other'];
if ($topic !== '' && !in_array($topic, $topics, true)) {
    $topic = 'Other';
}

if ($email === '' || !filter_var($email, FILTER_VALIDATE_EMAIL)) {
    fail(400, 'A valid email address is required.');
}
if (mb_strlen($message) < 10) {
    fail(400, 'The message is required (at least 10 characters).');
}
if (mb_strlen($name) > 120 || mb_strlen($email) > 254 || mb_strlen($organization) > 160
    || mb_strlen($message) > 5000) {
    fail(400, 'A field is longer than allowed.');
}

$clientIp = (string) ($_SERVER['REMOTE_ADDR'] ?? 'unknown');
if (!checkRateLimit($clientIp, $rateLimitMax, $rateLimitWindow)) {
    fail(429, 'Too many messages. Please wait a few minutes.');
}

// ------------------------------------------------------------------ email

$name = $name !== '' ? $name : 'Visitor';
$subject = $subjectPrefix . ($topic !== '' ? " — {$topic}" : '') . " — {$name}";

$body = implode("\n", [
    'New message from the onpremIA Labs website',
    '-------------------------------------------',
    'Name:         ' . $name,
    'Email:        ' . $email,
    'Organization: ' . ($organization !== '' ? $organization : '—'),
    'Topic:        ' . ($topic !== '' ? $topic : '—'),
    'Language:     ' . ($language !== '' ? $language : '—'),
    'IP:           ' . $clientIp,
    'Date:         ' . gmdate('Y-m-d H:i:s') . ' UTC',
    '',
    'Message:',
    $message,
    '',
    '-- ',
    'Reply to this email to answer ' . $name . ' directly.',
]);

$encodedName = mb_encode_mimeheader($name, 'UTF-8', 'B', "\r\n");
$headers = [
    'MIME-Version: 1.0',
    'Content-Type: text/plain; charset=UTF-8',
    'Content-Transfer-Encoding: 8bit',
    'From: onpremIA Labs <' . $envelopeFrom . '>',
    'Reply-To: ' . $encodedName . ' <' . $email . '>',
    'X-Mailer: onpremialabs-contact',
];

$sent = mail(
    $toEmail,
    mb_encode_mimeheader($subject, 'UTF-8', 'B', "\r\n"),
    $body,
    implode("\r\n", $headers),
    '-f' . $envelopeFrom
);

if (!$sent) {
    fail(500, 'The message could not be sent. Check the mail settings in Hostinger.');
}

respond(200, ['success' => true, 'message' => 'Message sent.']);

// ------------------------------------------------------------------ helpers

/** At most $max messages per IP in $windowSec seconds; the IP is stored hashed. */
function checkRateLimit(string $ip, int $max, int $windowSec): bool
{
    $dir = sys_get_temp_dir() . '/onpremialabs_contact_rate';
    if (!is_dir($dir) && !@mkdir($dir, 0700, true) && !is_dir($dir)) {
        return true; // no writable temp dir: do not block real visitors
    }

    $file = $dir . '/' . hash('sha256', $ip) . '.json';
    $now = time();
    $events = [];

    if (is_readable($file)) {
        $decoded = json_decode((string) file_get_contents($file), true);
        if (is_array($decoded)) {
            $events = array_values(array_filter(
                $decoded,
                static fn ($ts) => is_int($ts) && ($now - $ts) < $windowSec
            ));
        }
    }

    if (count($events) >= $max) {
        return false;
    }

    $events[] = $now;
    file_put_contents($file, json_encode($events), LOCK_EX);
    return true;
}
