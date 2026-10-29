**TOTP-only Authentication for users without HTTPS (on lemonseed.local domains)**

This is a standard and recommended pattern for high-security local setups. You can enable a feature called **"Require TOTP"** (or "Enforce TOTP") which forces all subsequent logins to use TOTP, while still allowing the initial setup via password.

### How It Works

1.  **Initial Setup (Password Only):**
    *   User logs in with username/password.
    *   Authelia detects that TOTP is not yet enabled for this user.
    *   Authelia displays the QR code and manual entry code.
    *   User scans/enters the code into their authenticator app.
    *   User verifies the first TOTP code.
    *   **TOTP is now enabled for this user.**

2.  **Subsequent Logins (TOTP Only):**
    *   User enters username.
    *   Authelia detects that TOTP is enabled for this user.
    *   Authelia **does not ask for a password**.
    *   Authelia asks for the **TOTP code**.
    *   User enters the 6-digit code.
    *   Login successful.

**Result:** The password is **never transmitted again** after the initial setup. Only the short-lived TOTP code is sent over the network.

### Configuration in Authelia

In your `configuration.yml`, you need to configure the TOTP section:

```yaml
totp:
  issuer: "LemonSeed"
  # This is the key setting:
  # When true, users MUST have TOTP enabled to log in.
  # If they don't have it yet, they can log in with password to set it up.
  # Once set up, they MUST use TOTP for all future logins.
  require: true
```

**Important Notes:**
*   **`require: true`** is the critical setting.
*   **Initial Setup:** Users who have not yet enabled TOTP can still log in with their password to complete the setup.
*   **After Setup:** Once TOTP is enabled, the password is **ignored** for that user. They must use TOTP.
*   **Fallback:** If a user loses their authenticator app, they **cannot** log in with their password. You will need to manually reset their TOTP in the database or via the admin API.

### Security Implications Over HTTP

This significantly improves your security posture:

| Scenario | What is Sent Over HTTP | Risk |
| :--- | :--- | :--- |
| **Initial Setup** | Username + Password | **High** (Password is in the clear) |
| **Subsequent Logins** | Username + TOTP Code | **Medium** (Code is in the clear, but expires in 30s) |

**Mitigations for Initial Setup:**
1.  **Physical Security:** Assume the user is physically in front of the appliance when setting up TOTP.
2.  **Rate Limiting:** Prevent brute-force attacks on the initial password.
3.  **One-Time Setup:** The password is only sent **once** per user. After that, it is never sent again.

**Mitigations for Subsequent Logins:**
1.  **Short-Lived Code:** TOTP codes expire in 30 seconds.
2.  **Single-Use:** Authelia can be configured to invalidate the code after one use.
3.  **Rate Limiting:** Prevent brute-force attacks on the TOTP code.

### Implementation Steps

1.  **Update `configuration.yml`:**
    ```yaml
    totp:
      issuer: "LemonSeed"
      require: true
    ```

2.  **Restart Authelia.**

3.  **User Flow:**
    *   User visits `http://lemonseed.local`.
    *   User enters username and password.
    *   Authelia shows the TOTP QR code.
    *   User scans the QR code.
    *   User enters the first TOTP code.
    *   User is logged in.
    *   **Next time:** User enters username and TOTP code only. No password.

### Handling Lost Authenticator Apps

Since the password is no longer accepted for users with TOTP enabled, you need a recovery mechanism:

1.  **Manual Reset:** You (the admin) can reset the user’s TOTP in the Authelia database or via the admin API.
2.  **Backup Codes:** Authelia does not natively support backup codes for TOTP. You may need to implement your own backup code system or instruct users to write down their TOTP secret.
3.  **Re-Enrollment:** You can temporarily disable TOTP for the user, allow them to log in with password, and re-enable TOTP.

### Final Recommendation

**Yes, configure Authelia with `totp.require: true`.**

This is the **best possible security model** for your HTTP-only constraint:
*   **Password is sent only once** (during initial setup).
*   **All subsequent logins use TOTP** (short-lived, single-use codes).
*   **Minimizes the window of vulnerability** for password theft.
*   **Works with Authelia’s existing features.**

**Document this clearly for your users:**
> "For security, we require TOTP (Time-Based One-Time Password) for all logins after initial setup. Your password is only used once to set up TOTP. After that, you will only need your authenticator app to log in."
