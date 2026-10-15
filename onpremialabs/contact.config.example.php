<?php
/**
 * Settings for contact.php. Copy this file to contact.config.php on the server
 * and adjust it there; contact.config.php is not kept in the repository.
 */
return [
    // Where the website's messages are delivered.
    'to_email' => 'info@onpremialabs.com',

    // The mailbox the email is sent from: a real mailbox of the domain in
    // Hostinger, so SPF and DMARC pass. The visitor goes in Reply-To.
    'envelope_from' => 'info@onpremialabs.com',

    // Subject prefix; the topic and the sender's name are appended.
    'subject' => '[onpremIA Labs] Contact',

    // At most this many messages per IP address in this many seconds.
    'rate_limit_max' => 5,
    'rate_limit_window_sec' => 600,

    // Pages allowed to post the form. Empty to allow any origin.
    'allowed_origins' => [
        'https://www.onpremialabs.com',
        'https://onpremialabs.com',
    ],
];
