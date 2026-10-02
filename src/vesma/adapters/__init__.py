"""In-process harness adapters over the Vesma provider contract.

ADR-0017 D1: adapters consume the memory server through the VesmaSDK
facade and the lifecycle hooks — never through bespoke transport. The
first adapter is the Hermes Agent memory-provider bridge
(:mod:`vesma.adapters.hermes`, vesma #125 Wave 5).

Naming note: this runtime package is ``vesma.adapters``, NOT
``vesma.integrations`` — the wheel force-includes the repo-root
``integrations/`` deploy artefacts (targets.yaml, skills, the Hermes
plugin) as the ``vesma/integrations`` DATA directory, so a Python
sub-package under that name would collide with every already-installed
copy (a namespace-package shadow, not a clean merge).
"""
