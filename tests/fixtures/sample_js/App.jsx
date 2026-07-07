// React component: renders <User/> (a capitalized JSX tag → a component reference) and a
// lowercase <div> (an HTML element → skipped). Also calls the imported `authenticate`.

import { authenticate } from "./auth";
import { User } from "./models";

export function App(props) {
  const user = authenticate(props.name);
  return (
    <div>
      <User name={user} />
    </div>
  );
}
