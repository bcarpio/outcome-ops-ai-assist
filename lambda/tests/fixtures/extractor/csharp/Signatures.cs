namespace Example;

public class Signatures
{
    public string Greet(string name, bool loud)
    {
        return name;
    }

    public void NoReturn(int a)
    {
    }

    public int WithGenerics(System.Collections.Generic.Dictionary<string, int> map, string fallback)
    {
        return map.Count;
    }
}
