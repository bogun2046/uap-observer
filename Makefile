.PHONY: dev dev-down dev-reset check lock staging-config wp10-runtime g10-25-object-store

dev:
	$(MAKE) -C platform dev

dev-down:
	$(MAKE) -C platform dev-down

dev-reset:
	$(MAKE) -C platform dev-reset

check:
	$(MAKE) -C platform check

wp10-runtime:
	$(MAKE) -C platform wp10-runtime

g10-25-object-store:
	$(MAKE) -C platform g10-25-object-store

lock:
	$(MAKE) -C platform lock

staging-config:
	$(MAKE) -C platform staging-config
