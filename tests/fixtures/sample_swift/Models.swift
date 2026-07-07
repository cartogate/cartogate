import Foundation

protocol Logger {
    func log()
}

class Base {
    func initialize() {}
}

class User: Base, Logger {
    let name: String

    init(name: String) {
        self.name = name
    }

    func isActive() -> Bool {
        return name.isEmpty == false
    }

    func log() {}
}

extension User {
    func greet() -> String {
        return name
    }
}
