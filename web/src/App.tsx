import { useState } from "react";
import Lobby from "./pages/Lobby";
import Room from "./pages/Room";

type Route = { page: "lobby" } | { page: "room"; gameId: string };

export default function App() {
  const [route, setRoute] = useState<Route>({ page: "lobby" });

  return (
    <div className="flex h-full flex-col">
      {route.page === "lobby" ? (
        <Lobby onEnter={(gameId) => setRoute({ page: "room", gameId })} />
      ) : (
        <Room gameId={route.gameId} onExit={() => setRoute({ page: "lobby" })} />
      )}
    </div>
  );
}
