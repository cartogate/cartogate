package app.models

open class Base {
    open fun init() {}
}

class User(val name: String) : Base() {
    fun isActive(): Boolean {
        return name.isNotEmpty()
    }
}
