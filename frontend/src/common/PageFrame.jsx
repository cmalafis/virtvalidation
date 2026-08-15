// Standard page chrome: breadcrumb, title, description, actions.
//
// Replaces the ~15 hand-rolled `function Shell({children})` + local
// `headerStyle` pairs that each page carried. Those existed because
// there was no layout route; now that AppLayout supplies the masthead
// and sidebar, a page only needs its own header band and body.

import { Link } from "react-router-dom";
import {
  Breadcrumb,
  BreadcrumbItem,
  Content,
  Flex,
  FlexItem,
  PageSection,
  Title,
} from "@patternfly/react-core";

/**
 * @param title       page heading
 * @param description optional one-line explanation under the heading
 * @param breadcrumbs [{ label, to? }] — the last entry renders as active
 * @param actions     right-aligned toolbar content (buttons)
 */
export default function PageFrame({
  title,
  description,
  breadcrumbs,
  actions,
  children,
}) {
  const crumbs = breadcrumbs ?? [];

  return (
    <>
      <PageSection type="breadcrumb" hasBodyWrapper={false}>
        {crumbs.length > 0 && (
          <Breadcrumb>
            {crumbs.map((crumb, i) => {
              const isLast = i === crumbs.length - 1;
              return (
                <BreadcrumbItem
                  key={`${crumb.label}-${i}`}
                  isActive={isLast}
                  {...(crumb.to && !isLast
                    ? { render: (props) => <Link to={crumb.to} {...props} /> }
                    : {})}
                >
                  {crumb.label}
                </BreadcrumbItem>
              );
            })}
          </Breadcrumb>
        )}
      </PageSection>

      <PageSection hasBodyWrapper={false}>
        <Flex
          justifyContent={{ default: "justifyContentSpaceBetween" }}
          alignItems={{ default: "alignItemsFlexStart" }}
          spaceItems={{ default: "spaceItemsMd" }}
        >
          <FlexItem>
            <Title headingLevel="h1">{title}</Title>
            {description && (
              <Content component="p" className="pf-v6-u-mt-sm pf-v6-u-color-200">
                {description}
              </Content>
            )}
          </FlexItem>
          {actions && <FlexItem>{actions}</FlexItem>}
        </Flex>
      </PageSection>

      {children}
    </>
  );
}
