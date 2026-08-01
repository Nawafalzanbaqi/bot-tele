"""One named production failure per module, reproduced and recovered.

The chaos suite asks "does the system survive being hit at random?". This suite
asks the narrower and more useful question: **for each failure we expect a
Raspberry Pi to actually produce, what exactly happens, and is it what the
Failure Matrix says happens?**

Every test here is therefore named after a real event - the power going off, the
card filling up, the kernel remounting the filesystem read-only, the clock
stepping backwards when NTP finally answers - and asserts the two things an
operator cares about: no work is silently lost, and no bytes are silently left
behind. ``docs/operations/failure-matrix.md`` is generated from what these
prove, so a row that changes has to change a test first.
"""
