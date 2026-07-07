namespace Sample.Models
{
    public class Base
    {
        public void Init() { }
    }

    public class User : Base
    {
        private readonly string _name;

        public User(string name)
        {
            _name = name;
        }

        public bool IsActive()
        {
            return _name != null;
        }
    }
}
