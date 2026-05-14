namespace Hyatt.Collisions
{
    public class A
    {
        public void Helper()
        {
            // empty
        }
    }

    public class B
    {
        public void Helper()
        {
            // empty
        }

        public void Caller()
        {
            Helper();
        }
    }
}
