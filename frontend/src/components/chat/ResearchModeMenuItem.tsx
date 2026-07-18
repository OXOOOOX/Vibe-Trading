import type { LucideIcon } from "lucide-react";
import { CheckCircle2, Target, Users } from "lucide-react";
import { useId } from "react";
import { useTranslation } from "react-i18next";

interface ExplainedMenuItemProps {
  label: string;
  onSelect: () => void;
  icon: LucideIcon;
  title: string;
  eyebrow: string;
  description: string;
  capabilities: readonly string[];
  footerLabel: string;
  footer: string;
  iconTone: string;
  eyebrowTone: string;
}

function ExplainedMenuItem({
  label,
  onSelect,
  icon: Icon,
  title,
  eyebrow,
  description,
  capabilities,
  footerLabel,
  footer,
  iconTone,
  eyebrowTone,
}: ExplainedMenuItemProps) {
  const hintId = useId();

  return (
    <div className="group relative after:absolute after:inset-y-0 after:left-full after:w-3">
      <button
        type="button"
        onClick={onSelect}
        aria-describedby={hintId}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm transition-colors hover:bg-muted focus-visible:bg-muted focus-visible:outline-none"
      >
        <Icon className="h-4 w-4" />
        {label}
      </button>

      <div
        id={hintId}
        role="tooltip"
        className="pointer-events-none invisible absolute bottom-full left-0 z-[60] mb-2 w-[min(20rem,calc(100vw-3rem))] translate-y-1 rounded-xl border border-border/80 bg-background p-4 text-foreground opacity-0 shadow-2xl ring-1 ring-black/10 backdrop-blur-xl transition duration-150 group-hover:visible group-hover:translate-y-0 group-hover:opacity-100 group-focus-within:visible group-focus-within:translate-y-0 group-focus-within:opacity-100 sm:bottom-1/2 sm:left-[calc(100%+0.75rem)] sm:mb-0 sm:translate-x-1 sm:translate-y-1/2 sm:group-hover:translate-x-0 sm:group-hover:translate-y-1/2 sm:group-focus-within:translate-x-0 sm:group-focus-within:translate-y-1/2"
      >
        <span
          aria-hidden="true"
          className="absolute -bottom-1.5 left-5 h-3 w-3 rotate-45 border-b border-r border-border/80 bg-background sm:-left-1.5 sm:bottom-1/2 sm:border-b-0 sm:border-l sm:border-t"
        />
        <div className="flex items-center gap-2">
          <span className={`rounded-md p-1.5 ${iconTone}`}>
            <Icon className="h-4 w-4" />
          </span>
          <div>
            <div className="text-sm font-semibold">{title}</div>
            <div className={`mt-0.5 text-[11px] font-medium ${eyebrowTone}`}>
              {eyebrow}
            </div>
          </div>
        </div>

        <p className="mt-3 text-xs leading-5 text-muted-foreground">
          {description}
        </p>

        <ul className="mt-3 space-y-2">
          {capabilities.map((capability) => (
            <li key={capability} className="flex gap-2 text-xs leading-5">
              <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-400" />
              <span>{capability}</span>
            </li>
          ))}
        </ul>

        <div className="mt-3 rounded-lg border bg-muted/70 px-3 py-2 text-[11px] leading-5 text-muted-foreground">
          <span className="font-semibold text-foreground">{footerLabel}</span>
          {footer}
        </div>
      </div>
    </div>
  );
}

interface MenuItemProps {
  onSelect: () => void;
}

export function ResearchGoalMenuItem({ onSelect }: MenuItemProps) {
  const { t } = useTranslation();
  const capabilities = [
    t("agent.researchGoalHint.capability1"),
    t("agent.researchGoalHint.capability2"),
    t("agent.researchGoalHint.capability3"),
  ];

  return (
    <ExplainedMenuItem
      label={t("agent.newResearchGoal")}
      onSelect={onSelect}
      icon={Target}
      title={t("agent.researchGoalHint.title")}
      eyebrow={t("agent.researchGoalHint.eyebrow")}
      description={t("agent.researchGoalHint.description")}
      capabilities={capabilities}
      footerLabel={t("agent.researchGoalHint.footerLabel")}
      footer={t("agent.researchGoalHint.footer")}
      iconTone="bg-primary/10 text-primary"
      eyebrowTone="text-primary"
    />
  );
}

export function SwarmTeamMenuItem({ onSelect }: MenuItemProps) {
  const { t } = useTranslation();
  const capabilities = [
    t("agent.swarmTeamHint.capability1"),
    t("agent.swarmTeamHint.capability2"),
    t("agent.swarmTeamHint.capability3"),
  ];

  return (
    <ExplainedMenuItem
      label={t("agent.runSwarmTeam")}
      onSelect={onSelect}
      icon={Users}
      title={t("agent.swarmTeamHint.title")}
      eyebrow={t("agent.swarmTeamHint.eyebrow")}
      description={t("agent.swarmTeamHint.description")}
      capabilities={capabilities}
      footerLabel={t("agent.swarmTeamHint.footerLabel")}
      footer={t("agent.swarmTeamHint.footer")}
      iconTone="bg-violet-500/10 text-violet-700 dark:text-violet-300"
      eyebrowTone="text-violet-700 dark:text-violet-300"
    />
  );
}
