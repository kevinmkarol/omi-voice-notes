# Security policy

Report a vulnerability privately through GitHub: the **Security** tab of this repository, then
**Report a vulnerability**. Please do not describe it in a pull request.

In scope: anything that exposes recordings, transcripts or the webhook token beyond the Mac they
live on, or lets a third party change what these tools do. The pendant itself accepts any
Bluetooth connection without pairing and runs firmware signed with Based Hardware's published key;
that is upstream's design (see the Security section of the README), and reports about it belong
with [BasedHardware/omi](https://github.com/BasedHardware/omi).

Only the latest commit on `main` is supported.
