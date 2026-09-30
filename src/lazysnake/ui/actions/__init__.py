"""Action mixins composed into :class:`lazysnake.ui.app.LazysnakeApp`.

Each module owns one panel's (or one feature area's) actions. The mixins
are plain classes: they reference app-provided infrastructure (``run_git``,
``mutate``, ``confirm``, ``refresh_state``, panels, ``snapshot``, ...) via
``self`` and never import each other. ``app.py`` composes them onto
``App`` in one class statement; Textual resolves ``action_*`` methods and
``@work`` workers through the resulting MRO unchanged.
"""
