// Application masthead.
//
// The old header carried product branding, four status counters, a
// hardcoded cluster name and six nav links. Counters moved to the
// Overview page and nav moved to the sidebar, so the masthead keeps only
// genuinely global concerns: identity, backend health, theme, and help.

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  Button,
  Divider,
  Dropdown,
  DropdownItem,
  DropdownList,
  Masthead,
  MastheadBrand,
  MastheadContent,
  MastheadLogo,
  MastheadMain,
  MastheadToggle,
  MenuToggle,
  PageToggleButton,
  Toolbar,
  ToolbarContent,
  ToolbarGroup,
  ToolbarItem,
} from "@patternfly/react-core";
import BarsIcon from "@patternfly/react-icons/dist/esm/icons/bars-icon";
import DesktopIcon from "@patternfly/react-icons/dist/esm/icons/desktop-icon";
import MoonIcon from "@patternfly/react-icons/dist/esm/icons/moon-icon";
import OutlinedQuestionCircleIcon from "@patternfly/react-icons/dist/esm/icons/outlined-question-circle-icon";
import SunIcon from "@patternfly/react-icons/dist/esm/icons/sun-icon";

import StatusLabel from "../common/StatusLabel";
import { applyTheme, getStoredTheme, subscribeToSystemTheme } from "../theme";
import { fetchJSON } from "../utils/fetchJSON";

const THEME_OPTIONS = [
  { key: "light", label: "Light", Icon: SunIcon },
  { key: "dark", label: "Dark", Icon: MoonIcon },
  { key: "system", label: "System", Icon: DesktopIcon },
];

function ThemeToggle() {
  const [theme, setTheme] = useState(getStoredTheme);
  const [open, setOpen] = useState(false);

  // Re-apply on OS change only while following the system setting.
  useEffect(() => {
    if (theme !== "system") return undefined;
    return subscribeToSystemTheme(() => applyTheme("system"));
  }, [theme]);

  const select = (key) => {
    applyTheme(key);
    setTheme(key);
    setOpen(false);
  };

  const active = THEME_OPTIONS.find((o) => o.key === theme) ?? THEME_OPTIONS[0];
  const ActiveIcon = active.Icon;

  return (
    <Dropdown
      isOpen={open}
      onOpenChange={setOpen}
      popperProps={{ position: "right" }}
      toggle={(ref) => (
        <MenuToggle
          ref={ref}
          variant="plain"
          onClick={() => setOpen((v) => !v)}
          isExpanded={open}
          aria-label={`Theme: ${active.label}`}
        >
          <ActiveIcon />
        </MenuToggle>
      )}
    >
      <DropdownList>
        {THEME_OPTIONS.map(({ key, label, Icon }) => (
          <DropdownItem
            key={key}
            icon={<Icon />}
            isSelected={key === theme}
            onClick={() => select(key)}
          >
            {label}
          </DropdownItem>
        ))}
      </DropdownList>
    </Dropdown>
  );
}

// Backend reachability + LLM posture. Polled slowly — this is an ambient
// indicator, not a dashboard. Failure renders as "Unreachable" rather
// than throwing, so a backend blip never blanks the whole shell.
function BackendStatus() {
  const [health, setHealth] = useState(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const data = await fetchJSON("/api/health/full");
        if (!cancelled) {
          setHealth(data);
          setFailed(false);
        }
      } catch {
        if (!cancelled) setFailed(true);
      }
    };
    load();
    const timer = setInterval(load, 60000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  if (failed) {
    return (
      <StatusLabel kind="status" value="failed">
        Backend unreachable
      </StatusLabel>
    );
  }
  if (!health) return null;

  // Backends report "online" / "offline" (app/api/health.py + the LLM
  // backends' health_check). Anything else is treated as not-online.
  const components = health?.components ?? {};
  const dbOk = (components?.database?.status ?? "") === "online";
  const llmOk = (components?.llm?.status ?? "") === "online";

  // Name the failing component — a bare "Degraded" sends the operator
  // hunting through Settings to find out what broke.
  const down = [!dbOk && "database", !llmOk && "LLM"].filter(Boolean);

  return (
    <StatusLabel kind="status" value={down.length === 0 ? "healthy" : "degraded"}>
      {down.length === 0 ? "Connected" : `Degraded: ${down.join(", ")}`}
    </StatusLabel>
  );
}

export default function AppMasthead() {
  return (
    <Masthead>
      <MastheadMain>
        <MastheadToggle>
          <PageToggleButton variant="plain" aria-label="Toggle navigation">
            <BarsIcon />
          </PageToggleButton>
        </MastheadToggle>
        <MastheadBrand>
          <MastheadLogo component={(props) => <Link to="/" {...props} />}>
            <span className="pf-v6-u-font-size-lg pf-v6-u-font-weight-bold">
              VirtValidate
            </span>
          </MastheadLogo>
        </MastheadBrand>
      </MastheadMain>

      <MastheadContent>
        <Toolbar isFullHeight isStatic>
          <ToolbarContent>
            <ToolbarGroup align={{ default: "alignEnd" }} gap={{ default: "gapSm" }}>
              <ToolbarItem>
                <BackendStatus />
              </ToolbarItem>
              <ToolbarItem>
                <Divider orientation={{ default: "vertical" }} />
              </ToolbarItem>
              <ToolbarItem>
                <ThemeToggle />
              </ToolbarItem>
              <ToolbarItem>
                <Button
                  variant="plain"
                  component="a"
                  href="https://github.com/cmalafis/virtvalidation/blob/main/docs/USER_GUIDE.md"
                  target="_blank"
                  rel="noreferrer"
                  aria-label="User guide"
                  icon={<OutlinedQuestionCircleIcon />}
                />
              </ToolbarItem>
            </ToolbarGroup>
          </ToolbarContent>
        </Toolbar>
      </MastheadContent>
    </Masthead>
  );
}
