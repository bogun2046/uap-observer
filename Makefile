.PHONY: dev dev-down dev-reset check lock staging-config

dev:
	$(MAKE) -C platform dev

dev-down:
	$(MAKE) -C platform dev-down

dev-reset:
	$(MAKE) -C platform dev-reset

check:
	$(MAKE) -C platform check

lock:
	$(MAKE) -C platform lock

staging-config:
	$(MAKE) -C platform staging-config
