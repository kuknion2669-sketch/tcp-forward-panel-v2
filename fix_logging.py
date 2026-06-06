#!/usr/bin/env python3
"""Fix bare excepts in haproxy_ctl.py and stats_collector.py"""
import re

# ── haproxy_ctl.py ──
with open('/root/tcp-panel-v2/haproxy_ctl.py') as f:
    h = f.read()

# Line ~78: except: pass → log it
h = h.replace(
    '                except:\n                    pass',
    '                except Exception:\n                    pass  # int() conversion failure, skip'
)
# Line ~131: except: pass
h = h.replace(
    '    except:\n        return {}',
    '    except Exception as _e:\n        log.warning(f"get_backend_stats failed: {_e}")\n        return {}'
)
# Line ~139: except:
h = h.replace(
    '        except:\n            pass',
    '        except Exception:\n            pass'
)
# Line ~151: except:
# This is get_connections
h = h.replace(
    '        except:\n            log.warning',
    '        except Exception:\n            log.warning'
)
# Make sure it has proper logging
# Line ~167-169: except in is_listening
h = h.replace(
    '    except:\n        return False',
    '    except Exception:\n        return False'
)
# Line ~246: except in reload shutdown sockets
h = h.replace(
    '        except:\n            pass',
    '        except Exception:\n            pass'
)

with open('/root/tcp-panel-v2/haproxy_ctl.py', 'w') as f:
    f.write(h)
print("Fixed haproxy_ctl.py excepts")

# ── stats_collector.py ──
with open('/root/tcp-panel-v2/stats_collector.py') as f:
    s = f.read()

s = s.replace(
    '    except Exception as e:\n            log.warning',
    '    except Exception:\n            log.warning'
)

# Fix bare except in auto-disable
s = s.replace(
    '        except:\n            pass',
    '        except Exception as _e:\n            log.warning(f"Auto-disable failed: {_e}")'
)

# Fix other bare excepts
s = s.replace(
    '            except:\n                pass',
    '            except Exception:\n                pass  # non-critical, skip'
)

with open('/root/tcp-panel-v2/stats_collector.py', 'w') as f:
    f.write(s)
print("Fixed stats_collector.py excepts")
